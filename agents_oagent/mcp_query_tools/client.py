# -*- coding: utf-8 -*-
"""按根任务生命周期管理 MCP 查询会话。

每个运行独立创建 stdio 服务进程，运行身份在装配期绑定。
SDK 通过 tools/list 发现工具，通过 tools/call 执行，输入与输出守卫负责查询前后处理。
会话关闭时回收本次进程，不跨运行复用身份或预算。"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from . import contract

SERVER_MODULE = 'agents_oagent.mcp_query_tools.server'
DEFAULT_BASE_URL = 'http://127.0.0.1:8099'
BASE_URL_ENV = 'OAGENT_TOOL_BASE_URL'
RUN_ID_ENV = 'OAGENT_MCP_RUN_ID'
# 数据服务读超时 60s（core/tool_client），MCP 会话读超时须更长：一次 call_tool 的耗时
# = 子进程内同步 httpx 查询 + 传输，不能让会话先于数据层超时。
SESSION_TIMEOUT_S = 120.0
_REPO_ROOT = Path(__file__).resolve().parents[2]


class MCPQueryError(RuntimeError):
    """MCP 适配层/传输层/装配故障（不是业务拒绝）。"""


def server_params(run_id: str, *, base_url: str | None = None,
                  python_executable: str | None = None) -> dict:
    """启动参数单点：命令、环境变量、工作目录（不在此处读凭据）。"""
    env = {str(k): str(v) for k, v in os.environ.items()}
    env[RUN_ID_ENV] = str(run_id)
    if base_url:
        env[BASE_URL_ENV] = str(base_url)
    # 子进程按模块名启动：把仓库根放进 PYTHONPATH，避免调用方 cwd 不在仓库根时 import 失败。
    pythonpath = env.get('PYTHONPATH') or ''
    root = str(_REPO_ROOT)
    if root not in [p for p in pythonpath.split(os.pathsep) if p]:
        env['PYTHONPATH'] = os.pathsep.join([root] + ([pythonpath] if pythonpath else []))
    env.setdefault('PYTHONUNBUFFERED', '1')
    return {'command': python_executable or sys.executable,
            'args': ['-m', SERVER_MODULE],
            'env': env,
            'cwd': root}


class MCPQueryClient:
    """一次根任务的 MCP 查询会话（`run_id` 在装配期固定，模型不可改）。

    生命周期三步：`configure_guardrails(...)`（装配层注入查询前后处理接入点，可选）→
    `start()`（起子进程、握手、核对工具面，交给 SDK 原生发现）→ `close()`（run 收尾）。
    会话建立后 `server` 即交给 `Agent.mcp_servers` 的 `MCPServerStdio`；本类不再转发调用。
    """

    def __init__(self, run_id: str, *, base_url: str | None = None,
                 python_executable: str | None = None,
                 session_timeout_s: float = SESSION_TIMEOUT_S,
                 use_structured_content: bool = True):
        self.run_id = str(run_id or '').strip()
        if not self.run_id:
            raise MCPQueryError('MCP 会话必须绑定根任务 run_id（不接受空值）')
        self._base_url = base_url
        self._python = python_executable
        self._timeout_s = session_timeout_s
        # Server 的 `content` 与 `structuredContent` 同源，取结构化字段可避免重复内容
        # （探针结论）；工具故障结果仍回落文本块，两种形状都由接入点还原。
        self._use_structured_content = bool(use_structured_content)
        self._input_guardrails: list | None = None
        self._output_guardrails: list | None = None
        self._server = None
        self.tool_names: list[str] = []

    def configure_guardrails(self, *, input_guardrails: list | None = None,
                             output_guardrails: list | None = None) -> 'MCPQueryClient':
        """装配层注入查询前后处理接入点（`ToolInputGuardrail` / `ToolOutputGuardrail`）。

        必须在 `start()` 之前调用：四个工具的全部预算、步骤、快照与模型投影都挂在这两个
        接入点上（已验证 SDK 扩展点），会话建立后不可再改。
        """
        if self._server is not None:
            raise MCPQueryError('MCP 会话已建立，不能再改接入点（须在 start() 之前注入）')
        self._input_guardrails = list(input_guardrails or []) or None
        self._output_guardrails = list(output_guardrails or []) or None
        return self

    @property
    def started(self) -> bool:
        return self._server is not None

    @property
    def server(self):
        """已连接的原生 MCP Server（`MCPServerStdio`）；`Agent.mcp_servers` 的注入物。"""
        if self._server is None:
            raise MCPQueryError('MCP 会话未建立（server 只在 start() 成功后可用）')
        return self._server

    async def start(self) -> 'MCPQueryClient':
        """起子进程、握手、核对工具面；任何一步失败都抛 `MCPQueryError`（不留半开会话）。"""
        try:
            from agents.mcp import MCPServerStdio
        except Exception as exc:            # noqa: BLE001  缺依赖/版本不兼容＝显式失败
            raise MCPQueryError('MCP 客户端依赖不可用：%s: %s'
                                % (type(exc).__name__, exc)) from exc
        server = MCPServerStdio(server_params(self.run_id, base_url=self._base_url,
                                              python_executable=self._python),
                                cache_tools_list=True,
                                client_session_timeout_seconds=self._timeout_s,
                                use_structured_content=self._use_structured_content,
                                tool_input_guardrails=self._input_guardrails,
                                tool_output_guardrails=self._output_guardrails,
                                name='oagent-query-tools')
        try:
            await server.connect()
            tools = await server.list_tools()
        except asyncio.CancelledError:
            # 连接期间取消时，临时 server 尚未交给会话对象，须在此清理后继续抛出取消。
            await self._safe_cleanup(server)
            raise
        except Exception as exc:            # noqa: BLE001
            await self._safe_cleanup(server)
            raise MCPQueryError('MCP 会话建立失败：%s: %s'
                                % (type(exc).__name__, exc)) from exc
        specs = contract.tool_specs()
        names = [str(tool.name) for tool in tools]
        if sorted(names) != sorted(specs):
            await self._safe_cleanup(server)
            raise MCPQueryError('MCP 工具面与合同不一致：server=%s contract=%s'
                                % (names, list(specs)))
        # 逐字段核对 inputSchema：模型面拿到的必须就是合同（不是 Server 侧第二份）。
        for tool in tools:
            expected = specs[str(tool.name)]['inputSchema']
            if tool.input_schema != expected:
                await self._safe_cleanup(server)
                raise MCPQueryError('MCP 工具 %s 的 inputSchema 与合同不一致' % tool.name)
        self._server = server
        self.tool_names = names
        return self

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            await self._safe_cleanup(server)

    @staticmethod
    async def _safe_cleanup(server) -> None:
        try:
            await server.cleanup()
        except Exception:                   # noqa: BLE001  关停失败不覆盖原始错误
            pass


__all__ = ['MCPQueryClient', 'MCPQueryError', 'server_params',
           'SERVER_MODULE', 'SESSION_TIMEOUT_S']
