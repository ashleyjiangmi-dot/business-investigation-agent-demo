import type { ReactNode } from 'react'
import { knowledgeFallbackLabel } from '../lib/knowledge-sources'
import { HIDDEN_REF_PLACEHOLDER } from '../lib/run-status'

/**
 * 正文 [E#] 短引用的可点击渲染器（不传=纯文本，逐字原样保留）。
 * 知识引用（`[GUIDE01]`/`[CASE04]`）复用同一渲染器：句柄=doc_id，返回 null 表示本 run
 * 未登记该文档（退纯文本泛化标签，不伪造可点来源）。
 * 第二参数=文字锚点的可见文字；不传时保持 `cite(handle)` 旧调用不变。
 */
export type CiteRender = (handle: string, text?: string) => ReactNode

const hiddenRef = HIDDEN_REF_PLACEHOLDER.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/**
 * `[[可见文字]][E#]` 完整文字锚点、Guard 隐藏后残留的 `[[可见文字]]〔无效引用，已隐藏〕`、
 * `[E#]` 数值证据、知识文档引用（方括号形态或正文里裸写的 GUIDE01/CASE04）。
 */
const CITE_RE = new RegExp(
  '\\[\\[([^\\[\\]\\n]+)\\]\\]\\[(E\\d+)\\]'
  + '|\\[\\[([^\\[\\]\\n]+)\\]\\]' + hiddenRef
  + '|\\[(E\\d+)\\]'
  + '|\\[((?:GUIDE|CASE)\\d+)\\]'
  + '|(?<![A-Za-z0-9_])((?:GUIDE|CASE)\\d+)(?![A-Za-z0-9_])',
  'g',
)

/**
 * 在纯文本段内切出文字锚点、`[E#]` 与知识文档引用 token。
 * - 完整文字锚点：整段交给 cite(handle, text)；无渲染器/渲染器拒绝时只去壳，句柄按旧形态逐字保留；
 * - Guard 隐藏后的 `[[文字]]` 残留：去壳为普通文字，保留异常占位，不产生入口；
 * - `[E#]`：cite 缺省时逐字原样保留（旧 SSE/ 形态不受影响）；
 * - 知识引用：无论 cite 是否传入都处理——已登记 → 交给 cite 渲染可点标签，未登记/无 cite
 *   → 纯文本泛化标签（不把内部 doc_id 铺到页面，也不伪造可点来源）。
 */
function pushCited(out: ReactNode[], text: string, key: string, cite?: CiteRender): void {
  let last = 0
  let m: RegExpExecArray | null
  let lastDoc: string | null = null
  CITE_RE.lastIndex = 0
  while ((m = CITE_RE.exec(text))) {
    const anchorText = m[1] ?? m[3]
    const anchorHandle = m[2]
    const docId = m[5] ?? m[6] ?? ''
    if (m[4] && !cite) continue          // 旧独立证据短号无渲染器：保持逐字（last 不动）
    // 同一位置连续重复引用同一文档 → 只保留一条（不同文档不合并）
    if (docId && docId === lastDoc && text.slice(last, m.index).trim() === '') {
      last = m.index + m[0].length
      continue
    }
    if (m.index > last) out.push(text.slice(last, m.index))
    if (anchorText !== undefined && anchorHandle) {
      const node = cite ? cite(anchorHandle, anchorText) : null
      if (node === null || node === undefined || node === false) {
        // 无详情/渲染器拒绝：去壳为普通文字，句柄仍走旧逐字形态（不伪造点击）。
        out.push(anchorText, `[${anchorHandle}]`)
      } else {
        out.push(<span key={`${key}@${m.index}`}>{node}</span>)
      }
    } else if (anchorText !== undefined) {
      // Guard 拒绝后只剩文字包围符：正常阅读，不产生可点击假入口。
      out.push(anchorText, HIDDEN_REF_PLACEHOLDER)
    } else if (m[4]) {
      out.push(<span key={`${key}@${m.index}`}>{cite!(m[4])}</span>)
    } else {
      const node = cite ? cite(docId) : null
      out.push(<span key={`${key}@${m.index}`}>{node ?? knowledgeFallbackLabel(docId)}</span>)
      lastDoc = docId
    }
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
}

/**
 * 完整渲染 Markdown 表格，外层容器负责横向滚动。
 * 普通文本、加粗、表格与列表共用引用渲染器；未提供渲染器时保留原文。
 * 文字锚点先于加粗切分，避免其中的星号被拆为独立标记。
 */
const INLINE_RE = new RegExp(
  '\\*\\*([^*]+)\\*\\*'
  + '|\\[\\[([^\\[\\]\\n]+)\\]\\]\\[(E\\d+)\\]'
  + '|\\[\\[([^\\[\\]\\n]+)\\]\\]' + hiddenRef,
  'g',
)

export function inline(text: string, cite?: CiteRender): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  INLINE_RE.lastIndex = 0
  while ((m = INLINE_RE.exec(text))) {
    if (m.index > last) pushCited(out, text.slice(last, m.index), `${m.index}p`, cite)
    if (m[1] !== undefined) {
      const inner: ReactNode[] = []
      pushCited(inner, m[1], `${m.index}s`, cite)
      out.push(<strong key={m.index}>{inner}</strong>)
    } else {
      pushCited(out, m[0], `${m.index}a`, cite)
    }
    last = m.index + m[0].length
  }
  if (last < text.length) pushCited(out, text.slice(last), 'tail', cite)
  return out
}

const cellsOf = (line: string) =>
  line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim())
const isRule = (line: string) => /^\|[\s:|-]+\|?$/.test(line.trim())

export default function MarkdownLite({ text, cite }: { text: string; cite?: CiteRender }) {
  const lines = String(text ?? '').replace(/\r\n/g, '\n').split('\n')
  const blocks: ReactNode[] = []
  // 块级 key 统一自增：与行号 i 是两套体系，混用会撞号（React "same key" 告警）
  let blockId = 0
  let para: string[] = []
  const flushPara = () => {
    if (!para.length) return
    const rows = para
    blocks.push(
      <p key={blockId++}>
        {rows.map((l, i) => (
          <span key={i}>
            {inline(l, cite)}
            {i < rows.length - 1 && <br />}
          </span>
        ))}
      </p>,
    )
    para = []
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const t = line.trim()
    if (!t) {
      flushPara()
      continue
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(t)
    if (h) {
      flushPara()
      const Tag = (`h${Math.min(h[1].length + 1, 5)}` as 'h2' | 'h3' | 'h4' | 'h5')
      blocks.push(<Tag key={blockId++}>{inline(h[2], cite)}</Tag>)
      continue
    }
    if (t.startsWith('|')) {
      flushPara()
      const rows: string[][] = []
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        if (!isRule(lines[i])) rows.push(cellsOf(lines[i]))
        i++
      }
      i--
      const [head, ...body] = rows
      if (!head?.length || body.length === 0) continue
      blocks.push(
        <div className="md-table-scroll" key={blockId++}>
          <table>
            <thead>
              <tr>{(head ?? []).map((c, j) => <th key={j}>{inline(c, cite)}</th>)}</tr>
            </thead>
            <tbody>
              {body.map((r, k) => (
                <tr key={k}>
                  {r.map((c, j) => (
                    <td key={j}>{inline(c, cite)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }
    if (/^(-{3,}|\*{3,})$/.test(t)) {
      flushPara()
      blocks.push(<hr key={blockId++} />)
      continue
    }
    if (t.startsWith('>')) {
      flushPara()
      blocks.push(<blockquote key={blockId++}>{inline(t.replace(/^>\s?/, ''), cite)}</blockquote>)
      continue
    }
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line)
    if (bullet) {
      flushPara()
      const items: string[] = []
      while (i < lines.length) {
        const m2 = /^\s*[-*]\s+(.*)$/.exec(lines[i])
        if (!m2) break
        items.push(m2[1])
        i++
      }
      i--
      blocks.push(
        <ul key={blockId++}>
          {items.map((it, k) => (
            <li key={k}>{inline(it, cite)}</li>
          ))}
        </ul>,
      )
      continue
    }
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line)
    if (numbered) {
      flushPara()
      const items: string[] = []
      while (i < lines.length) {
        const m2 = /^\s*\d+[.)]\s+(.*)$/.exec(lines[i])
        if (!m2) break
        items.push(m2[1])
        i++
      }
      i--
      blocks.push(
        <ol key={blockId++}>
          {items.map((it, k) => (
            <li key={k}>{inline(it, cite)}</li>
          ))}
        </ol>,
      )
      continue
    }
    para.push(t)
  }
  flushPara()
  return <div className="md">{blocks}</div>
}
