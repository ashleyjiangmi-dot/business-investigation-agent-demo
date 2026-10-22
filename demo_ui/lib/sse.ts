export type DifyEvent = {
  event?: string
  [key: string]: unknown
}

/** 按 SSE 帧（空行分隔）取 data: 行；心跳与非 JSON 帧跳过。服务端与浏览器共用。 */
export function takeFrames(buffer: string): { frames: DifyEvent[]; rest: string } {
  const frames: DifyEvent[] = []
  let rest = buffer
  for (;;) {
    const end = rest.indexOf('\n\n')
    if (end < 0) break
    const rawFrame = rest.slice(0, end)
    rest = rest.slice(end + 2)
    for (const line of rawFrame.split('\n')) {
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).trim()
      if (!payload) continue
      try {
        frames.push(JSON.parse(payload) as DifyEvent)
      } catch {
        /* 非 JSON（心跳/注释）不参与展示 */
      }
    }
  }
  return { frames, rest }
}

/** 整段文本 → 帧数组（离线重放用；运行面走 takeFrames 增量切分）。 */
export const framesOf = (text: string): DifyEvent[] => takeFrames(text).frames
