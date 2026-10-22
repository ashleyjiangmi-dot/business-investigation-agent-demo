import type { ReactNode } from 'react'
import type { CompactReport } from '@/lib/report'
import type { StructuredOutcome, StructuredView } from '@/lib/structured-report'
// 使用相对路径，便于独立加载组件进行静态渲染。
import { parseMdTable, readingOf, splitCaveats } from '../lib/structured-report'
import { productSubjectDisplay, readableFreeText } from '../lib/product-display'
import type { TraceScope } from '../lib/trace'
import type { ChangeItem, FindingUnit, MdTable } from '@/lib/structured-report'
import MarkdownLite, { inline, type CiteRender } from './MarkdownLite'

/** 根据单一商品 ID 展示目录中的名称与消歧标签；身份不明确时保留模型原文。 */
function ProductNameCell({ name, scope, className }: { name: string; scope?: TraceScope; className: string }) {
  const d = productSubjectDisplay(name, scope)
  return (
    <span className={className} title={d.replaced ? `原文：${name}` : undefined}>
      {inline(d.text)}
    </span>
  )
}

/** 变化行的两期读数文本（全部为原文子串拼接，见 structured-report.ts）。 */
function DeltaReading({ it, cite }: { it: ChangeItem; cite?: CiteRender }) {
  return <span className="delta-reading">{inline(readingOf(it), cite)}</span>
}

function DeltaFooter({ it, cite }: { it: ChangeItem; cite?: CiteRender }) {
  const parts: string[] = []
  if (it.change) parts.push(it.change)
  if (it.rate) parts.push(it.rate)
  if (it.direction) parts.push(`方向${it.direction}`)
  const line = parts.join(' · ')
  return (
    <div className="delta-foot">
      {line && <span className="delta-change">{inline(line, cite)}</span>}
      {it.refs.trim() && <span className="delta-refs">{inline(it.refs.trim(), cite)}</span>}
    </div>
  )
}

/** ① 指标变化卡：名称、两期数值、变化、引用——全部逐字子串；[E#] 可点开对应引用详情。 */
function MetricCards({ items, cite }: { items: ChangeItem[]; cite?: CiteRender }) {
  return (
    <div className="metric-cards">
      {items.map((it, i) => (
        <div className="metric-card" key={i}>
          <div className="metric-name">{inline(it.name, cite)}</div>
          <DeltaReading it={it} cite={cite} />
          <DeltaFooter it={it} cite={cite} />
        </div>
      ))}
    </div>
  )
}

/** ② 商品比较表：商品（目录名展示）、两期读数、变化、依据余句（如有方向也在此列）；宽表横向滚动。 */
function CompareTable({ items, cite, scope }: { items: ChangeItem[]; cite?: CiteRender; scope?: TraceScope }) {
  const hasBasis = items.some((it) => it.basis)
  return (
    <div className="table-scroll">
      <table className="compare-table">
        <thead>
          <tr>
            <th>商品</th>
            <th>两期读数</th>
            <th>变化</th>
            {hasBasis && <th>说明与依据</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((it, i) => (
            <tr key={i}>
              <td className="cell-name"><ProductNameCell name={it.name} scope={scope} className="pname" /></td>
              <td><DeltaReading it={it} cite={cite} /></td>
              <td>
                <DeltaFooter it={it} cite={cite} />
              </td>
              {hasBasis && <td className="cell-basis">{inline(it.basis, cite)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** ③ 候选商品列表：名称（目录名展示）、入选条件与依据（依据为条目逐字余句）。 */
function CandidateList({ items, cite, scope }: { items: ChangeItem[]; cite?: CiteRender; scope?: TraceScope }) {
  return (
    <ol className="candidates">
      {items.map((it, i) => (
        <li className="candidate" key={i}>
          <div className="cand-name"><ProductNameCell name={it.name} scope={scope} className="pname" /></div>
          <div className="cand-reading"><DeltaReading it={it} cite={cite} /><DeltaFooter it={it} cite={cite} /></div>
          <div className="cand-basis">{inline(it.basis, cite)}</div>
        </li>
      ))}
    </ol>
  )
}

/** Markdown 表格条目（关键发现中的表格体）：表头+数据行逐字搬用。 */
function MdTableBlock({ table, cite, scope }: { table: MdTable; cite?: CiteRender; scope?: TraceScope }) {
  const rt = (text: string) => readableFreeText(text, scope)
  return (
    <div className="table-scroll">
      <table className="compare-table">
        {table.headers.length > 0 && (
          <thead>
            <tr>{table.headers.map((h, j) => <th key={j}>{inline(rt(h), cite)}</th>)}</tr>
          </thead>
        )}
        <tbody>
          {table.rows.map((r, i) => (
            <tr key={i}>{r.map((c, j) => <td key={j}>{inline(rt(c), cite)}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * 主回答（summary）里的完整 Markdown 表格：整张表是一个不可拆分的展示单元，
 * 默认折叠，点击后完整展开；收起时整张表一起隐藏。
 * 展开后复用结果卡现有表格组件（表头/数据行/引用逐字，宽表横向滚动）。
 */
function SummaryTableBlock({ table, cite, scope }: { table: MdTable; cite?: CiteRender; scope?: TraceScope }) {
  return (
    <details className="more summary-table">
      <summary>查看数据表格（{table.rows.length} 行）</summary>
      <MdTableBlock table={table} cite={cite} scope={scope} />
    </details>
  )
}

/** 普通正文条目：保留行内加粗与换行，不重排。 */
function PlainItem({ text, cite, scope }: { text: string; cite?: CiteRender; scope?: TraceScope }) {
  const lines = readableFreeText(text, scope).split('\n')
  return (
    <p className="plain-finding">
      {lines.map((l, i) => (
        <span key={i}>
          {inline(l, cite)}
          {i < lines.length - 1 && <br />}
        </span>
      ))}
    </p>
  )
}

/** 段落显示保留原文换行（服务端 summary 用单换行衔接列表项，不再被折叠成文本坨）。 */
function ParaLines({ text, cite }: { text: string; cite?: CiteRender }) {
  const lines = text.split('\n')
  return (
    <>
      {lines.map((l, i) => (
        <span key={i}>
          {inline(l, cite)}
          {i < lines.length - 1 && <br />}
        </span>
      ))}
    </>
  )
}

/**
 * 局部引用异常的处理（2026-10-20 可读性收口）：正文里未通过核验的引用已由服务端隐藏，
 * 受影响的最小单元由  局部提示（`〔局部提示：……〕`）就地标明；本组件不再把整段/
 * 整条折叠进「依据待核实」，也不因结论区出现一处异常引用就否定整段结论。正文原样展示，
 * 局部提示随正文可见。
 */

/**
 * 主回答（summary）按原顺序渲染：
 * - 完整 Markdown 表格 → 整表一个展示单元、默认折叠（`查看数据表格`），展开后一次性显示
 *   全部表头/行列/引用（不再按普通段落切成「表头在折叠入口上方、数据行在下方」）；
 * - 普通段落 → 前 2 段默认可见（首段建立层级），其余原文进折叠区；
 * - 折叠区遇到表格时就地收口，表格始终落在原文对应位置，表格前后的经营分析与建议顺序不变。
 */
function ConclusionBody({ paras, cite, scope }: { paras: string[]; cite?: CiteRender; scope?: TraceScope }) {
  const rt = (text: string) => readableFreeText(text, scope)
  const nodes: ReactNode[] = []
  let folded: string[] = []
  let shown = 0
  const flush = () => {
    if (!folded.length) return
    nodes.push(
      <details className="more" key={`folded-${nodes.length}`}>
        <summary>展开结论其余 {folded.length} 段</summary>
        {folded.map((p, i) => (
          <p key={i}><ParaLines text={rt(p)} cite={cite} /></p>
        ))}
      </details>,
    )
    folded = []
  }
  for (const p of paras) {
    const table = parseMdTable(p)
    if (table) {
      flush()
      nodes.push(<SummaryTableBlock key={`table-${nodes.length}`} table={table} cite={cite} scope={scope} />)
      continue
    }
    if (shown < 2) {
      nodes.push(
        <p key={`p-${nodes.length}`} className={shown === 0 ? 'conclusion-lead' : undefined}>
          <ParaLines text={rt(p)} cite={cite} />
        </p>,
      )
      shown++
      continue
    }
    folded.push(p)
  }
  flush()
  return <div className="conclusion">{nodes}</div>
}

function FindingUnits({ view, cite, scope }: { view: StructuredView; cite?: CiteRender; scope?: TraceScope }) {
  const renderUnit = (u: FindingUnit, i: number) => {
    switch (u.kind) {
      case 'md-table': return <MdTableBlock key={i} table={u.table} cite={cite} scope={scope} />
      case 'md-block': return <MarkdownLite key={i} text={readableFreeText(u.text, scope)} cite={cite} />
      case 'metric-cards': return <MetricCards key={i} items={u.items} cite={cite} />
      case 'compare-table': return <CompareTable key={i} items={u.items} cite={cite} scope={scope} />
      case 'candidates': return <CandidateList key={i} items={u.items} cite={cite} scope={scope} />
      default: return <PlainItem key={i} text={u.text} cite={cite} scope={scope} />
    }
  }
  return (
    <>
      {view.findingUnits.map((u, i) => renderUnit(u, i))}
      {view.findingOverflow.length > 0 && (
        <details className="more">
          <summary>展开其余 {view.findingOverflow.length} 条关键发现</summary>
          {view.findingOverflow.map((f, i) => <PlainItem key={i} text={f} cite={cite} scope={scope} />)}
        </details>
      )}
    </>
  )
}

/**
 * 展示结论、数据块、必要限制与下一步，并保留完整分析入口。
 * 有类型化数据块时将详细发现收起，避免同一读数重复铺开。
 * 只在可确认商品身份的结构化字段中替换目录名称，完整正文保持原样。
 * Markdown 表格作为整体折叠，避免被摘要段数限制拆开。
 */
function StructuredResult({
  view,
  scope,
  cite,
  typed,
  hasDisplayBlocks,
}: {
  view: StructuredView
  /** 服务端目录用于商品名称与消歧标签展示，缺失时保留原文。 */
  scope?: TraceScope
  cite?: CiteRender
  /** 类型化展示块（display_blocks 视图）；null=本 run 无块，结构与  前逐字一致 */
  typed?: ReactNode
  /** 范围面板不算业务结果块；page 显式传块存在性，旧调用方回退 typed。 */
  hasDisplayBlocks?: boolean
}) {
  const paras = view.summaryParas
  const hasTyped = hasDisplayBlocks ?? typed != null
  const findingsAll = view.findingUnits.length + view.findingOverflow.length
  // 完整 summary 默认可见前两段普通文字（首段为结论、次段为必要解释），其余按需展开；
  // 完整 Markdown 表格整表默认折叠（见 ConclusionBody）；运营自由文本做目录门控可读化，
  // 关键发现/下一步溢出等保留原文回查。
  const rt = (text: string) => readableFreeText(text, scope)
  const conclusionNode = <ConclusionBody paras={paras} cite={cite} scope={scope} />

  // IX04R §业务限制就近、取数详情按需：首屏限制只留影响本轮判断的业务限制（范围/可比性/
  // 未知项/替代比较），「Demo 模拟口径」自证与底层行数/字段覆盖等取数细节收进按需折叠区——
  // 页面级已统一说明模拟口径（app/page.tsx），限制段不再重复自证。**一条不删**被移出的
  // 条目原文逐字保留在折叠区（`splitCaveats` 只决定呈现位置）。
  //  增量：问题范围自证（「其余指标未在本轮查询范围」类通用声明）同入口径折叠区；业务
  // 限制为 0 条时不再挂「必要限制（0 条）」空头（ix05 真机 ui-d965 纯读数对比题）。
  const caveatSplit = splitCaveats(view.caveats)
  const caveatsNode = view.caveats.length > 0 && (
    <div className="block">
      {/* C03：限制全部是口径/范围自证时不再挂「必要限制（0 条）」空头——自证原文逐字在
          下方「口径与取数说明」折叠区（一条不删），首屏只留影响本轮判断的业务限制。 */}
      {caveatSplit.business.length > 0 && <h3>必要限制（{caveatSplit.business.length} 条）</h3>}
      <ul className="caveats-visible">
        {caveatSplit.business.map((c, i) => (
          <li key={i}>{inline(rt(c), cite)}</li>
        ))}
      </ul>
      {caveatSplit.restated.length > 0 && (
        <details className="caveats-restated">
          <summary>口径与取数说明（{caveatSplit.restated.length} 条）</summary>
          <ul>
          {caveatSplit.restated.map((c, i) => (
              <li key={i}>{inline(rt(c), cite)}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
  const nextStepsNode = view.nextSteps.length > 0 && (
    <div className="block">
      <h3>下一步</h3>
      <ol className="actions">
        {view.nextSteps.map((a, i) => (
          <li key={i}>{inline(rt(a), cite)}</li>
        ))}
      </ol>
      {view.nextStepsOverflow.length > 0 && (
        <details className="more">
          <summary>展开其余 {view.nextStepsOverflow.length} 条下一步</summary>
          <ol className="actions">
            {view.nextStepsOverflow.map((a, i) => (
            <li key={i}>{inline(rt(a), cite)}</li>
            ))}
          </ol>
        </details>
      )}
    </div>
  )
  const findingsNode = (view.findingUnits.length > 0 || view.findingOverflow.length > 0) && (
    <FindingUnits view={view} cite={cite} scope={scope} />
  )

  if (hasTyped) {
    // 影响判断的补充信息（限制/下一步）紧跟结论与关键结果；
    // 关键发现是完整分析的主体，默认收纳、逐字可展开，不与类型化结果并排重复铺开数字。
    return (
      <div className="result structured">
        {conclusionNode}
        {typed}
        {caveatsNode}
        {nextStepsNode}
        {findingsNode && (
          <div className="block">
            <details className="full-analysis">
              <summary>补充发现（{findingsAll} 条）</summary>
              {findingsNode}
            </details>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="result structured">
      {conclusionNode}

      {typed}

      {findingsNode && (
        <div className="block">
          <h3>关键发现</h3>
          {findingsNode}
        </div>
      )}

      {nextStepsNode}

      {caveatsNode}
    </div>
  )
}


export default function ResultCard({
  report,
  structured = { status: 'absent' },
  scope,
  cite,
  typed,
  hasDisplayBlocks,
}: {
  report: CompactReport
  /** 无结构化报告时使用正文视图。 */
  structured?: StructuredOutcome
  /** 商品目录用于比较表和候选列表名称展示。 */
  scope?: TraceScope
  /** 正文 [E#] 的可点击渲染（缺省=纯文本 [E#]， 形态） */
  cite?: CiteRender
  /** 类型化展示块（display_blocks 视图）；null=本 run 无块，三个分支的原有内容不变 */
  typed?: ReactNode
  /** 显式区分业务结果块与仅用于范围信息的 typed 节点。 */
  hasDisplayBlocks?: boolean
}) {
  //  §4：调用方显式声明投影不可用（hasDisplayBlocks=false）时，即使误传 typed 节点
  // 也不渲染——整份降级只走一次完整规范正文，不同时呈现已保留的部分业务块
  // （page.tsx 已保证 typed 与 hasDisplayBlocks 一致，此处为双保险）。
  const typedNode = hasDisplayBlocks === false ? null : typed
  const rt = (text: string) => readableFreeText(text, scope)
  if (hasDisplayBlocks === false) {
    return (
      <div className="result full-business-body">
        <MarkdownLite text={rt(report.businessText)} cite={cite} />
      </div>
    )
  }
  if (structured.status === 'structured') {
    return (
      <StructuredResult
        view={structured.view}
        scope={scope}
        cite={cite}
        typed={typedNode}
        hasDisplayBlocks={hasDisplayBlocks}
      />
    )
  }
  if (structured.status === 'conflict') {
    // 冲突=结构化字段与 detail_md 不一致：主区直显完整有效正文（卡片 §4「回退完整详情」），
    // 不经过  精简解析的默认裁剪；内部展示异常只在 page.tsx 记 console。
    return (
      <div className="result conflict-fallback">
        {typedNode}
        <MarkdownLite text={rt(structured.detail_md)} cite={cite} />
      </div>
    )
  }
  const omitted = report.unparsed ? [] : [
    ...report.skipped.map((title) => `「${title}」`),
    ...(report.findingOverflow ? [`其余 ${report.findingOverflow} 条关键发现`] : []),
    ...(report.actionOverflow ? [`其余 ${report.actionOverflow} 条下一步`] : []),
  ]
  return (
    <div className="result">
      {report.unparsed ? (
        <MarkdownLite text={rt(report.fallbackText)} cite={cite} />
      ) : (
        <div className="conclusion">
          {(() => {
            // 结论超过 2 段时默认只显示前 2 段，其余原样折叠（不改写、不摘要）；
            // 首段用 conclusion-lead 建立视觉层级，段内原文换行以换行显示。
            const paras = (report.conclusion ?? '').split('\n\n').filter(Boolean)
            const rest = paras.slice(2)
            return (
              <>
                {paras.slice(0, 2).map((p, i) => (
                  <p key={i} className={i === 0 ? 'conclusion-lead' : undefined}><ParaLines text={rt(p)} cite={cite} /></p>
                ))}
                {rest.length > 0 && (
                  <details className="more">
                    <summary>展开结论其余 {rest.length} 段</summary>
                    {rest.map((p, i) => (
                      <p key={i}><ParaLines text={rt(p)} cite={cite} /></p>
                    ))}
                  </details>
                )}
              </>
            )
          })()}
        </div>
      )}

      {typedNode}

      {!report.unparsed && report.findings.length > 0 && (
        <div className="block">
          <h3>关键发现</h3>
          <ul className="findings">
            {report.findings.map((f, i) => (
              <li key={i}>
                {f.parts.map((p, j) => (
                  <span key={j}>{inline(rt(p), cite)}</span>
                ))}
              </li>
            ))}
          </ul>
          {report.findingNotes.length > 0 && (
            <details className="finding-notes">
              <summary>关键发现补充说明（{report.findingNotes.length} 段）</summary>
              {report.findingNotes.map((note, i) => <MarkdownLite key={i} text={rt(note)} cite={cite} />)}
            </details>
          )}
        </div>
      )}

      {!report.unparsed && report.actions.length > 0 && (
        <div className="block">
          <h3>下一步</h3>
          <ol className="actions">
            {report.actions.map((a, i) => (
              <li key={i}>{inline(rt(a), cite)}</li>
            ))}
          </ol>
        </div>
      )}

      {!report.unparsed && report.boundary.length > 0 && (
        <details className="boundary">
          <summary>必要边界（{report.boundary.length} 条）</summary>
          <ul>
            {report.boundary.map((b, i) => (
              <li key={i}>{inline(rt(b), cite)}</li>
            ))}
          </ul>
        </details>
      )}

      {omitted.length > 0 && (
        <p className="hint omitted-note">完整调查中保留：{omitted.join('、')}</p>
      )}
    </div>
  )
}
