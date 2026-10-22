/**
 * 单浏览器会话内的「同一次调查」身份与恢复。
 *
 * 服务端在**进程存活期内**按 `run_id` 记忆运行状态与已装配终态（`agents_oagent/server.py`
 * 的 `RunRegistry`）。页面的责任只有两件：①提交前生成一个稳定 `run_id`，连同原问题与结构化
 * 输入存进 `sessionStorage`——同一标签页刷新/组件重挂载后仍读得到；②重新挂载时用同一 ID 走
 * 只读 GET 取回，而不是重新 POST。不建立跨设备运行历史、不落盘。
 *
 * 这里刻意只做纯数据搬运与确定性比较，不碰网络；页面与离线 harness 共用同一实现。
 */

export const RUN_SESSION_KEY = 'oagent.active_run'

export type RunSession = {
  runId: string
  /** 当次实际提交的结构化输入（提交值合同，非表单当前值） */
  inputs: Record<string, string>
  /** 原问题，供恢复后摘要行与提示使用 */
  question: string
}

/** 最小 sessionStorage 形状（浏览器真身与测试替身共用；不引 window 类型）。 */
export type SessionStore = {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

/** 客户端生成的稳定运行标识：前缀 `ui-` 表明来源是页面（服务端只当键值，不消费前缀）。 */
export function newRunId(): string {
  const rand =
    typeof globalThis.crypto?.randomUUID === 'function'
      ? globalThis.crypto.randomUUID().replace(/-/g, '').slice(0, 12)
      : Math.random().toString(16).slice(2, 14)
  return `ui-${rand}`
}

/** 结构化输入的确定性比较（键序无关；页面判「同一次调查」只看这份输入）。 */
export function sameInputs(a: Record<string, string>, b: Record<string, string>): boolean {
  const ka = Object.keys(a).sort()
  const kb = Object.keys(b).sort()
  if (ka.length !== kb.length) return false
  return ka.every((k, i) => k === kb[i] && a[k] === b[k])
}

/** 读取本会话已存的活动运行；缺失/损坏一律当「没有」处理（不猜、不半恢复）。 */
export function loadRunSession(store: SessionStore | null | undefined): RunSession | null {
  if (!store) return null
  const raw = store.getItem(RUN_SESSION_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as Partial<RunSession>
    if (typeof parsed?.runId !== 'string' || !parsed.runId.trim()) return null
    const inputs = parsed.inputs && typeof parsed.inputs === 'object'
      ? Object.fromEntries(
          Object.entries(parsed.inputs as Record<string, unknown>)
            .filter(([, v]) => typeof v === 'string')
            .map(([k, v]) => [k, v as string]),
        )
      : {}
    return { runId: parsed.runId, inputs, question: typeof parsed.question === 'string' ? parsed.question : '' }
  } catch {
    return null
  }
}

export function saveRunSession(store: SessionStore | null | undefined, session: RunSession): void {
  if (!store) return
  try {
    store.setItem(RUN_SESSION_KEY, JSON.stringify(session))
  } catch {
    /* 隐私模式/配额受限：不阻断调查，只是本次不可恢复 */
  }
}

/**
 * 清掉本会话的活动运行记录。只在服务端明确证明该运行不可恢复（404）时调用；
 * 「发起新调查」不清会话——输入未变＝取回同一次运行，改过输入＝提交时生成新 run_id 并覆盖本记录。
 */
export function clearRunSession(store: SessionStore | null | undefined): void {
  if (!store) return
  try {
    store.removeItem(RUN_SESSION_KEY)
  } catch {
    /* 同上：清理失败不阻断 */
  }
}
