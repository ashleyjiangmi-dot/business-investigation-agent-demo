# -*- coding: utf-8 -*-
"""顺序启动本地数据服务、Agent 服务与 Demo UI，并等待就绪、合流日志及回收子进程。

用法：在仓库根执行 python dev.py。启动与环境配置见根 README。
前一个服务未就绪时不启动下一个；Ctrl+C 关闭本次启动的进程树。
本脚本不安装依赖、不配置凭据、不发起模型调用。"""
from __future__ import annotations

import collections
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEMO_UI_DIR = REPO_ROOT / 'demo_ui'

DATA_PORT = 8099
# 运行时端口可由 AGENTS_OAGENT_SERVING_PORT 覆盖。
RUNTIME_PORT = int(os.environ.get('AGENTS_OAGENT_SERVING_PORT') or '8327')
UI_PORT = 3000
UI_URL = 'http://localhost:%d' % UI_PORT


class DevError(RuntimeError):
    """启动失败原因（带可读原因，直接打印给使用者）。"""


# —— 终端着色（Windows 控制台需显式开 VT；不可用则退化为纯文本前缀）——
def _enable_vt() -> bool:
    if os.name != 'nt':
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


_COLOR = _enable_vt() and sys.stdout.isatty()
_RESET = '\x1b[0m'
_TAG_STYLE = {
    'data': ('data   ', '\x1b[36m'),
    'runtime': ('runtime', '\x1b[35m'),
    'ui': ('ui     ', '\x1b[32m'),
    'dev': ('dev    ', '\x1b[33m'),
}


def log(tag: str, text: str = '') -> None:
    """按来源标签打印一行（stdout 编码兜底，避免子进程罕见字符打断输出）。"""
    label, color = _TAG_STYLE[tag]
    prefix = ('%s[%s]%s' % (color, label, _RESET)) if _COLOR else ('[%s]' % label)
    line = ('%s %s' % (prefix, text)).rstrip()
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or 'utf-8'
        print(line.encode(enc, 'replace').decode(enc, 'replace'), flush=True)


def _decode(raw: bytes) -> str:
    """子进程输出按 utf-8 / gbk 依次尝试（Windows 下 Python 管道输出通常为本地编码）。"""
    for enc in ('utf-8', 'gbk'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


# 探测请求不走系统代理：本机代理会劫持 localhost 调用（同因）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 400
    except Exception:
        return False


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(('127.0.0.1', port)) == 0


class Service:
    """一个被管起来的服务进程：启动 / 日志合流 / 就绪等待 / 关闭。"""

    def __init__(self, tag, title, cmd, display, cwd, ready_urls, ready_timeout, env_extra=None):
        self.tag = tag
        self.title = title
        self.cmd = cmd              # list（逐参数）或 str（整串命令行，见 _start_ui）
        self.display = display
        self.cwd = cwd
        self.ready_urls = list(ready_urls)
        self.ready_timeout = ready_timeout
        self.env_extra = dict(env_extra or {})
        self.proc = None
        self.tail = collections.deque(maxlen=60)

    def start(self) -> None:
        env = os.environ.copy()
        env.update(self.env_extra)
        kwargs = {}
        creationflags = 0
        if os.name == 'nt':
            # 独立进程组：Ctrl+C 不直接打到子进程，由本脚本统一收尾。
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        self.proc = subprocess.Popen(
            self.cmd, cwd=str(self.cwd), env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=creationflags, **kwargs)
        for stream in (self.proc.stdout, self.proc.stderr):
            threading.Thread(target=self._pump, args=(stream,), daemon=True).start()
        log(self.tag, '已启动：%s' % self.display)

    def _pump(self, stream) -> None:
        try:
            for raw in iter(stream.readline, b''):
                text = _decode(raw).rstrip('\r\n')
                if not text:
                    continue
                self.tail.append(text)
                log(self.tag, text)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def tail_text(self, n: int = 12) -> str:
        lines = list(self.tail)[-n:]
        if not lines:
            return '    （无输出）'
        return '\n'.join('    ' + line for line in lines)

    def wait_ready(self) -> None:
        deadline = time.time() + self.ready_timeout
        while True:
            if self.proc.poll() is not None:
                raise DevError('%s 进程已退出（returncode=%s）\n最近日志：\n%s'
                               % (self.title, self.proc.returncode, self.tail_text()))
            for url in self.ready_urls:
                if _http_ok(url):
                    log(self.tag, '就绪：%s' % url)
                    return
            if time.time() > deadline:
                raise DevError('%s 在 %d 秒内未就绪（探测 %s）\n最近日志：\n%s'
                               % (self.title, self.ready_timeout,
                                  self.ready_urls[0], self.tail_text()))
            time.sleep(0.5)


def _npm_cmd() -> str | None:
    """定位 npm：Windows 上 npm 是 .cmd 批处理（CreateProcess 不能直接执行 .cmd）。"""
    if os.name == 'nt':
        return shutil.which('npm.cmd') or shutil.which('npm')
    return shutil.which('npm')


def _preflight() -> None:
    """检查启动依赖与配置；缺失时按 README 提示补救方法，不自动安装或修改配置。"""
    if _npm_cmd() is None:
        raise DevError('未找到 npm（Demo UI 需要 Node.js/npm）：请先安装 Node.js')
    if not (DEMO_UI_DIR / 'node_modules').is_dir():
        raise DevError('demo_ui/node_modules 不存在：请先在 demo_ui/ 执行 npm install')
    if not (DEMO_UI_DIR / '.env.local').is_file():
        raise DevError('demo_ui/.env.local 不存在：请先在 demo_ui/ 执行 '
                       'cp .env.example .env.local（README §3）')
    if not (REPO_ROOT / 'agents_oagent' / '.env.local').is_file():
        raise DevError('agents_oagent/.env.local 不存在：模型凭据未配置（README §3 凭据装载）')
    if not (os.environ.get('DIAG_TOOLS_API_KEY') or '').strip():
        raise DevError('环境变量 DIAG_TOOLS_API_KEY 未设置：:8099 与 runtime 共用的桥接凭据缺失'
                       '（Windows：setx DIAG_TOOLS_API_KEY "<值>" 后新开终端，README §3）')


def _check_port_free(port: int, what: str) -> None:
    if _port_busy(port):
        raise DevError('端口 %d（%s）已被占用：请先关掉已在运行的实例——dev.py 不复用已有实例，'
                       '否则就绪探测会连到旧实例造成假成功' % (port, what))


def _start_data() -> Service:
    _check_port_free(DATA_PORT, '数据服务')
    svc = Service(
        tag='data',
        title='数据服务 core.data.tool_server (:%d)' % DATA_PORT,
        cmd=[sys.executable, '-m', 'core.data.tool_server'],
        display='%s -m core.data.tool_server' % sys.executable,
        cwd=REPO_ROOT,
        ready_urls=['http://127.0.0.1:%d/healthz' % DATA_PORT],
        ready_timeout=60,
    )
    svc.start()
    return svc


def _start_runtime() -> Service:
    _check_port_free(RUNTIME_PORT, 'Agent Runtime')
    svc = Service(
        tag='runtime',
        title='Agent Runtime agents_oagent.serve_live (:%d)' % RUNTIME_PORT,
        cmd=[sys.executable, '-m', 'agents_oagent.serve_live'],
        display='%s -m agents_oagent.serve_live' % sys.executable,
        cwd=REPO_ROOT,
        # 与 README 一致：NO_PROXY 防系统代理劫持 localhost；HF_* 关联网探测避免起服卡顿。
        env_extra={
            'HF_HUB_OFFLINE': '1',
            'TRANSFORMERS_OFFLINE': '1',
            'NO_PROXY': '127.0.0.1,localhost',
        },
        ready_urls=['http://127.0.0.1:%d/healthz' % RUNTIME_PORT],
        ready_timeout=300,
    )
    svc.start()
    return svc


def _start_ui() -> Service:
    _check_port_free(UI_PORT, 'Demo UI')
    npm = _npm_cmd()
    env_extra = {}
    if RUNTIME_PORT != 8327:
        # 仅当显式改了 runtime 端口时同步前端基址（.env.local 默认仍指 :8327）。
        env_extra['OAGENT_SERVING_URL'] = 'http://127.0.0.1:%d' % RUNTIME_PORT
        log('dev', 'AGENTS_OAGENT_SERVING_PORT=%d：前端 OAGENT_SERVING_URL 同步指向该端口'
            % RUNTIME_PORT)
    if os.name == 'nt':
        # Windows：npm 是 .cmd 批处理，CreateProcess 不能直接执行 ⇒ 经 comspec 走一层。
        # 用整串命令行而非列表：cmd 的引号规则与 list2cmdline 的转义规则不兼容。
        comspec = os.environ.get('COMSPEC') or 'cmd.exe'
        cmd = '%s /d /s /c ""%s" run dev"' % (comspec, npm)
        display = '"%s" run dev' % npm
    else:
        cmd = [npm, 'run', 'dev']
        display = '%s run dev' % npm
    svc = Service(
        tag='ui',
        title='Demo UI next dev (:%d)' % UI_PORT,
        cmd=cmd,
        display=display,
        cwd=DEMO_UI_DIR,
        env_extra=env_extra,
        ready_urls=['http://localhost:%d/' % UI_PORT, 'http://127.0.0.1:%d/' % UI_PORT],
        ready_timeout=300,
    )
    svc.start()
    return svc


def _supervise(services) -> None:
    """就绪后驻留：任一服务中途退出即报错并整体收尾。"""
    while True:
        time.sleep(0.5)
        for svc in services:
            rc = svc.proc.poll()
            if rc is not None:
                raise DevError('%s 进程意外退出（returncode=%s）\n最近日志：\n%s'
                               % (svc.title, rc, svc.tail_text()))


def _kill_tree(proc) -> None:
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        # /T 连子进程一起收（npm -> node），/F 免残留。
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()


def _shutdown(services) -> None:
    alive = [svc for svc in services if svc.proc is not None and svc.proc.poll() is None]
    for svc in alive:
        log(svc.tag, '正在关闭：%s' % svc.title)
        _kill_tree(svc.proc)
    for svc in alive:
        try:
            svc.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log(svc.tag, '未在 10 秒内退出，已强制结束')
    for svc in services:
        if svc.proc is None:
            continue
        for stream in (svc.proc.stdout, svc.proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
    if alive:
        log('dev', '全部服务已关闭。')


def _banner() -> None:
    log('dev', '=' * 58)
    log('dev', ' 三个服务已就绪')
    log('dev', '   数据服务       http://127.0.0.1:%d' % DATA_PORT)
    log('dev', '   Agent Runtime  http://127.0.0.1:%d' % RUNTIME_PORT)
    log('dev', '   Demo UI        %s' % UI_URL)
    log('dev', ' 浏览器请用 localhost（127.0.0.1 会被 Next dev 跨源守卫拦 HMR）')
    log('dev', ' 按 Ctrl+C 关闭全部服务')
    log('dev', '=' * 58)


def main() -> int:
    log('dev', '仓库根目录：%s' % REPO_ROOT)
    try:
        _preflight()
    except DevError as exc:
        log('dev', '前置检查失败：%s' % exc)
        return 1

    services = []
    try:
        services.append(_start_data())
        services[-1].wait_ready()
        services.append(_start_runtime())
        services[-1].wait_ready()
        services.append(_start_ui())
        services[-1].wait_ready()
        _banner()
        _supervise(services)
    except KeyboardInterrupt:
        log('dev', '收到 Ctrl+C，正在关闭全部服务……')
    except DevError as exc:
        log('dev', '启动失败：%s' % exc)
        return 1
    finally:
        _shutdown(services)
    return 0


if __name__ == '__main__':
    sys.exit(main())
