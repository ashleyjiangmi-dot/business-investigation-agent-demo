import { mkdir, open, writeFile } from 'node:fs/promises'
import type { FileHandle } from 'node:fs/promises'
import path from 'node:path'
import { buildEntryFields, type EntryField } from '@/lib/entry-fields'
import { takeFrames } from '@/lib/sse'
import { filterEvent } from '@/lib/trace-filter'

/** 上游为 Agents SDK 的 SSE 服务。 */
const SERVING_URL = process.env.OAGENT_SERVING_URL || 'http://127.0.0.1:8327'
const RAW_DIR =
  process.env.OAGENT_RAW_DIR ||
  path.resolve(process.cwd(), '../agents_oagent/evidence/demo_ui/runs')
/** 原始事件记录开关：默认关闭。OAGENT_RAW_DUMP=1 时落 raw/wire SSE 与独立 user_question 记录。 */
const DUMP = process.env.OAGENT_RAW_DUMP === '1'

function checkInputs(fields: EntryField[], inputs: Record<string, string>): string | null {
  for (const f of fields) {
    const v = inputs[f.name]
    if (f.required && !String(v ?? '').trim()) return `缺少必填输入：${f.name}`
    if (f.kind === 'select' && v && !f.options.includes(v)) return `${f.name} 不在下发选项内：${v}`
    if (f.maxLength && String(v ?? '').length > f.maxLength) return `${f.name} 超过 ${f.maxLength} 字`
  }
  const unknown = Object.keys(inputs).filter((k) => !fields.some((f) => f.name === k))
  return unknown.length ? `未知输入字段：${unknown.join(',')}` : null
}

export async function POST(request: Request) {
  const body = (await request.json()) as { inputs?: Record<string, string>; user?: string; run_id?: string }
  const inputs = body.inputs ?? {}

  let fields
  try {
    fields = (await buildEntryFields()).fields
  } catch (e) {
    return Response.json({ error: `入口字段对账失败：${e instanceof Error ? e.message : e}` }, { status: 502 })
  }
  const invalid = checkInputs(fields, inputs)
  if (invalid) return Response.json({ error: invalid }, { status: 400 })

  // 客户端生成的稳定 run_id 原样透传（服务端据此复用同一执行/拒绝输入冲突）；
  // 缺省时不传——服务端自建 `as-…`，旧调用形状与旧行为逐字不变。
  const runId = typeof body.run_id === 'string' ? body.run_id.trim() : ''
  const upstream = await fetch(`${SERVING_URL}/v1/oagent/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      inputs,
      user: body.user || 'demo-ui-local',
      ...(runId ? { run_id: runId } : {}),
    }),
  })

  if (!upstream.ok || !upstream.body) {
    // 上游的**请求级**拒绝（409 run_id_conflict 等）原样带回来，页面据此给明确提示，
    // 不吞成泛化 502（ §行为 2–3：冲突与未知都要透明交给用户）。
    const detail = await upstream.text()
    let errorCode = ''
    try {
      errorCode = String((JSON.parse(detail) as { error_code?: string }).error_code ?? '')
    } catch {
      /* 非 JSON 上游错误：只带 HTTP 码。 */
    }
    if (upstream.status === 409 || upstream.status === 404) {
      return Response.json({ error: errorCode || `服务层返回 HTTP ${upstream.status}`, detail }, { status: upstream.status })
    }
    return Response.json(
      { error: `服务层返回 HTTP ${upstream.status}`, detail: detail.slice(0, 2000) },
      { status: 502 },
    )
  }

  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..+/, '')
  const dump = DUMP
    ? {
        raw: path.resolve(RAW_DIR, `stream_${stamp}.sse`),
        wire: path.resolve(RAW_DIR, `stream_${stamp}.wire.sse`),
        request: path.resolve(RAW_DIR, `stream_${stamp}.request.json`),
      }
    : null
  const headers: Record<string, string> = {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    'X-Accel-Buffering': 'no',
  }
  if (dump) {
    await mkdir(RAW_DIR, { recursive: true })
    await writeFile(dump.request, JSON.stringify({ user_question: inputs.user_question }), 'utf-8')
    headers['X-Stream-Files'] = `stream_${stamp}.sse / stream_${stamp}.wire.sse`
  }

  return new Response(proxyStream(upstream.body, dump), { headers })
}

/**
 *  只读恢复：把页面刷新/重挂载后的同一 `run_id` 转给服务端只读 GET，重放同一次运行的
 * 已有帧与终态。本代理**不创建任务**——是否已有该运行、是否仍在跑全由服务端登记表回答；
 * 未知 ID（含服务重启后旧 ID）上游 404 原样带回，页面提示无法恢复且不再 POST。
 */
export async function GET(request: Request) {
  const runId = (new URL(request.url).searchParams.get('run_id') ?? '').trim()
  if (!runId) return Response.json({ error: '缺少 run_id' }, { status: 400 })

  let upstream: Response
  try {
    upstream = await fetch(`${SERVING_URL}/v1/oagent/run/${encodeURIComponent(runId)}`, {
      method: 'GET',
      headers: { Accept: 'text/event-stream' },
    })
  } catch (e) {
    // 服务层暂时不可达 ≠ 该运行不存在：给 502 让页面按「暂时取不回」处理（保留会话里的 run_id），
    // 不能与 404「记录确实没有」混为一谈。
    return Response.json(
      { error: '服务层暂时不可达', detail: e instanceof Error ? e.message : String(e) },
      { status: 502 },
    )
  }

  if (!upstream.ok || !upstream.body) {
    const detail = await upstream.text()
    let errorCode = ''
    try {
      errorCode = String((JSON.parse(detail) as { error_code?: string }).error_code ?? '')
    } catch {
      /* 非 JSON 上游错误：只带 HTTP 码。 */
    }
    return Response.json(
      { error: errorCode || `服务层返回 HTTP ${upstream.status}`, detail: detail.slice(0, 2000) },
      { status: upstream.status === 404 ? 404 : 502 },
    )
  }

  return new Response(proxyStream(upstream.body, null), {
    headers: {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      'X-Accel-Buffering': 'no',
    },
  })
}

const encoder = new TextEncoder()
const decoder = new TextDecoder()

/**
 * 上游 SSE（服务层已按新 schema 源头过滤）→ 键面守卫兜底 → 重放。原始帧只在守卫前存在，
 * 浏览器只收到这里 enqueue 的白名单事件字节。
 */
function proxyStream(
  upstream: ReadableStream<Uint8Array>,
  dump: { raw: string; wire: string } | null,
) {
  let raw: FileHandle | null = null
  let wire: FileHandle | null = null
  return new ReadableStream<Uint8Array>({
    async start(controller) {
      if (dump) {
        raw = await open(dump.raw, 'w')
        wire = await open(dump.wire, 'w')
      }
      const reader = upstream.getReader()
      let buffer = ''
      const emit = async (text: string) => {
        controller.enqueue(encoder.encode(text))
        if (wire) await wire.writeFile(text)
      }
      try {
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          if (raw && value) await raw.writeFile(value)
          buffer += decoder.decode(value, { stream: true })
          const { frames, rest } = takeFrames(buffer)
          buffer = rest
          for (const frame of frames) {
            const wireEvent = filterEvent(frame)
            if (wireEvent) await emit(`data: ${JSON.stringify(wireEvent)}\n\n`)
          }
        }
        if (buffer.trim()) console.warn(`事件流尾部 ${buffer.length} 字符未成帧，已丢弃`)
      } catch (e) {
        await emit(
          `data: ${JSON.stringify({ event: 'error', data: { reason_class: 'proxy_stream_error' } })}\n\n`,
        )
      } finally {
        await raw?.close()
        await wire?.close()
        controller.close()
      }
    },
  })
}
