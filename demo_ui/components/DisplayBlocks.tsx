import type { BlockGroupView, BlockView, CohortView, FactGroupView, FactRowView, GroupTotalView, ToolRankingView, SinglePeriodTotalsView } from '../lib/display-blocks'
import { buildBlockGroups, collectGroupTotals, duplicateCohortKeys, firstScreenRender, COHORT_INLINE_LIMIT, FACT_ROW_LIMIT } from '../lib/display-blocks'
import { EMPTY_SCOPE, type TraceScope } from '../lib/trace'
import { type CiteRender } from './MarkdownLite'

/**
 *  类型化展示块组件 +  原型样式与就近依据 +  范围面板 +  业务结果可读性。
 *
 * **核验与降级机制减法（2026-10-15）**主回答角色/块级可信评级/模型自写 checks/自由文字
 * focus/why 与自由文字数字核验面全部退出。组件只消费 `lib/display-blocks.ts` 的**数据面**
 * 视图：正文（四节 `answer_md`）是唯一业务叙述，数据块只提供可回查事实、合法封闭复算与
 * cohort 权威集合；真实引用/计算问题由块级 `data_issues` 就近局部说明（业务语言，内部
 * 句柄/字段/合同细节不下发）。
 *
 * 组织（保留）：
 * - **品类两期对比**各 blocking 服务端复算的 `recomputed.group_totals` 按目录可信 `category`
 *   跨块并列；来源范围＋指标＋周期＋复算事实全等的汇总只展示一条（不同商品子集分别保留）；
 * - **条件筛选首屏给名单**少量权威命中名单（≤ COHORT_INLINE_LIMIT）直接全露，较长名单先露
 *   前若干项＋「查看全部 N 项」（展开后仍是完整权威全集）；同一权威集合不重复铺；
 * - **行式读数**首屏关键读数行只铺前 FACT_ROW_LIMIT 条； §1：完整结果的
 *   「查看全部 N 项 / 查看剩余 N 项」入口留在对应业务模块内（不只交给依据区），
 *   完整读数表同时仍在数据依据作底稿回查；
 * - **渐进收纳**同类型连续块合并为一组；跨组按 handle 首次出现去重（事实不删）；
 * - **局部说明**data_issues 按块就近展示（只呈现影响业务结果/范围的限制）。
 */

function isTotalsOnlyFallback(v: BlockView): boolean {
  return v.groupTotals.length > 0
    && !v.cohort
    && v.factRows.length === 0
    && v.changeRows.length === 0
    && v.medians.length === 0
    && v.ranks.length === 0
    && v.toolRankings.length === 0
    && v.toolMedians.length === 0
    && v.singlePeriodTotals.length === 0
    && v.dataIssues.length === 0
}

function rowsForBlock(g: BlockGroupView, block: BlockView): FactRowView[] {
  return g.changeRows.filter((row) => row.key.startsWith(`${block.key}:`))
}

function shownHandlesForGroup(g: BlockGroupView): Set<string> {
  const shown = new Set<string>()
  if (g.type === 'candidate_list' && g.blocks.length > 1) {
    for (const block of g.blocks) {
      for (const handle of firstScreenRender(rowsForBlock(g, block)).shownHandles) shown.add(handle)
    }
  } else {
    for (const handle of firstScreenRender(g.changeRows).shownHandles) shown.add(handle)
  }
  return shown
}

export default function DisplayBlocks({
  views,
  cite,
  scope,
}: {
  views: BlockView[]
  cite?: CiteRender
  /** 服务端目录（首屏商品/指标/周期可读化）；缺省=原文逐字（旧调用不变） */
  scope?: TraceScope
}) {
  const effScope = scope ?? EMPTY_SCOPE
  if (!views.length) return null
  // 纯兜底块只喂 GroupTotalsPanel，不参与组渲染（组渲染按去掉它们后的视图）
  const bodyViews = views.filter((v) => !isTotalsOnlyFallback(v))
  // 第二参传全量视图——纯合计兜底块的分组合计仍是合法承载面，跨类型覆盖判定
  // （单期合计是否已被两期对比完整承载）必须看全量，不能只看分组输入。
  const groups = buildBlockGroups(bodyViews, views)
  const totals = collectGroupTotals(views)
  const dupCohorts = duplicateCohortKeys(bodyViews)
  // 首屏优先级局部排序：重点商品（候选列表）回答「哪些商品值得关注」，提到其余组之前。
  // 只调**组的视觉顺序**——不改 buildBlockGroups 的输入序（跨组 handle 去重归属不变）。
  const orderedGroups = [
    ...groups.filter((g) => g.type === 'candidate_list'),
    ...groups.filter((g) => g.type !== 'candidate_list'),
  ]
  // 跨品类对比时「品类两期对比」提到数据区之前（问题本身是跨品类比较）。
  const trustedCats = new Set(totals.filter((t) => t.categoryTrusted).map((t) => t.categoryText))
  const crossCategoryComparison = trustedCats.size >= 2
  //  跨组依据区统一归属：首屏实际出现过的 handle 收成**全局**集合（不只本组）——
  // 依据区据此排除，同一事实不在「A 组首屏」与「B 组依据表」各铺一遍（去重只作用于呈现，
  // 不删任何事实：完整读数表仍含全部未被首屏展示的行）。
  const globalShownHandles = new Set<string>()
  for (const g of orderedGroups) {
    for (const h of shownHandlesForGroup(g)) globalShownHandles.add(h)
    for (const membership of g.candidateMemberships) {
      for (const member of membership.members) {
        if (member.handle) globalShownHandles.add(member.handle)
      }
    }
    for (const ranking of g.toolRankings) {
      for (const row of ranking.rows) {
        for (const fact of row.relatedFacts) globalShownHandles.add(fact.handle)
      }
    }
  }
  const hasDataArea = (totals.length > 0 && !crossCategoryComparison) || orderedGroups.length > 0
  return (
    <div className="typed-blocks">
      {crossCategoryComparison && <GroupTotalsPanel totals={totals} scope={effScope} />}
      {hasDataArea && (
        <section className="tb-data-area" aria-label="关键对象数据">
          <div className="tb-head"><span className="tb-kind">关键对象数据</span></div>
          {totals.length > 0 && !crossCategoryComparison && <GroupTotalsPanel totals={totals} scope={effScope} />}
          {orderedGroups.map((g) => (
            <TypedGroup key={g.key} g={g} cite={cite} dupCohorts={dupCohorts}
              shownHandles={globalShownHandles} />
          ))}
        </section>
      )}
    </div>
  )
}

function declaredMetaPeriod(meta: string | null): string | null {
  return meta?.split('\u3000').find((segment) => segment.startsWith('周期：'))?.slice('周期：'.length) ?? null
}

/** 事实行的依据入口：cite 缺省（旧流无详情数据）= 不渲染；注册项由页面给 chip/状态标记。 */
function RefCite({ handle, cite }: { handle: string; cite?: CiteRender }) {
  if (!cite) return null
  return <span className="tb-cite">{cite(handle)}</span>
}

/** 指标读数的依据入口：读数本身作为文字入口；无 cite / 未登记详情时退回纯文本，不伪造点击。 */
function ReadingCite({ handle, text, cite }: { handle: string; text: string; cite?: CiteRender }) {
  if (!cite) return <>{text}</>
  const node = cite(handle, text)
  if (node === null || node === undefined || node === false) return <>{text}</>
  return <>{node}</>
}

/**
 * 首屏关键结果行。**商品是经营阅读的第一视觉单位**：同一商品的多条读数聚为一段
 * （段内给指标、读数与实际周期），而不是把 41 条事实当 41 个结果平铺。名称取
 * 「同指标→对象名 / 同对象→指标名 / 都不同→对象 · 指标」的确定性归约；读数与变化逐字来自
 * wire 值，不铺重复口径。成对事实给「基期 → 对比期」与变化幅度；单期事实只给该行读数
 * （实际周期就近标注），不伪造变化栏。
 *  §1：`FACT_ROW_LIMIT`/`KR_GROUP_LIMIT`/`KR_READINGS_PER_OBJECT` 只限制**默认展开量**——
 * 完整结果的「查看全部 / 查看剩余 N 项」入口必须留在**本业务模块**内：平铺超限给
 * 「查看剩余 N 项」，商品段超限给「查看该商品其余 N 条读数」＋「查看全部 N 项（其余商品）」，
 * 不把剩余项只交给依据区才能找到（完整读数表同时仍在数据依据，作底稿回查）。
 */
function FactRows({ rows, cite, declaredPeriod }: {
  rows: FactRowView[]
  cite?: CiteRender
  declaredPeriod?: string | null
}) {
  if (!rows.length) return null
  const productIds = new Set(rows.map((r) => r.productId).filter((id): id is string => Boolean(id)))
  const periodStart = (label: string) => label.match(/^(\d{4}-\d{2}-\d{2})(?:~\d{4}-\d{2}-\d{2})?$/)?.[1] ?? null
  const datedRows = productIds.size === 1 && rows.every((r) => periodStart(r.periodText))
    ? rows.map((row, index) => ({ row, index, start: periodStart(row.periodText)! }))
      .sort((a, b) => a.start.localeCompare(b.start) || a.index - b.index)
      .map(({ row }) => row)
    : rows
  const hasResolvedPeriod = (periodText: string) => periodText !== '—' && periodText !== ' → '
  const mixedCaliber = new Set(datedRows.filter((r) => hasResolvedPeriod(r.periodText)).map((r) => r.periodText)).size > 1
    || new Set(datedRows.map((r) => r.kindText)).size > 1
  const periodDeclared = Boolean(declaredPeriod && !mixedCaliber
    && datedRows.every((row) => row.periodText === declaredPeriod || !hasResolvedPeriod(row.periodText)))
  const render = firstScreenRender(datedRows)
  const changeCell = (r: FactRowView) => (
    <span className="kr-change">
      {r.hasPair ? (
        <>
          <span className={`direction-value direction-${r.tone}`}>{r.pctText || '—'}</span>
          {r.tone === 'unknown' && r.stateText && r.stateText !== '正常读数' && !r.readingText.includes(r.stateText) && (
            <span className="kr-state">{r.stateText}</span>
          )}
        </>
      ) : (r.tone === 'unknown' && r.stateText && r.stateText !== '正常读数' && !r.readingText.includes(r.stateText)
        ? <span className="kr-state">{r.stateText}</span>
        : null)}
    </span>
  )

  // A single object's compact multi-period comparison reads better as a time × metric grid.
  // Only use it for a complete, unambiguous small grid; incomplete data keeps the normal evidence rows.
  const periods = [...new Set(datedRows.map((r) => r.periodText))]
  const metrics = [...new Set(datedRows.map((r) => r.metricText))]
  const matrixRows = periods.map((periodText) => ({
    periodText,
    start: periodStart(periodText),
    cells: metrics.map((metricText) => datedRows.filter((r) => r.periodText === periodText && r.metricText === metricText)),
  }))
  const canRenderTimeMatrix = productIds.size === 1 && datedRows.every((r) => r.productId && !r.hasPair && !r.seriesText)
    && periods.length >= 2 && metrics.length >= 1
    && matrixRows.every((row) => row.start && row.cells.every((cell) => cell.length === 1))
    && datedRows.length === periods.length * metrics.length
  if (canRenderTimeMatrix) {
    const ordered = [...matrixRows].sort((a, b) => a.start!.localeCompare(b.start!))
    return (
      <div className="table-scroll kr-time-matrix">
        <table className="compare-table">
          <thead>
            <tr><th>实际周期</th>{metrics.map((m) => <th key={m}>{m}</th>)}</tr>
          </thead>
          <tbody>
            {ordered.map((row) => (
              <tr key={row.periodText}>
                <th scope="row">{row.periodText}</th>
                {row.cells.map((matches, i) => {
                  const r = matches[0]
                  const value = r.targetNumeric
                    ? `${r.targetNumText}${r.unitText ? ` ${r.unitText}` : ''}`
                    : r.stateText || '暂无可用数据'
                  return (
                    <td key={`${row.periodText}-${metrics[i]}`}>
                      <ReadingCite handle={r.handle} text={value} cite={cite} />
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  if (render.grouped) {
    // 商品为段：段头是商品名，段内是该商品的读数行（读数本身即证据入口）。
    const renderReading = (r: FactRowView) => {
      const values = r.readingText
      return (
        <li className="kr-reading" key={r.key}>
          <span className="kr-reading-metric">{r.metricNameText}</span>
          <span className="kr-values">
            <ReadingCite handle={r.handle} text={values} cite={cite} />
            {!periodDeclared && <span className="kr-caliber">{r.periodText}{r.kindText ? ` · ${r.kindText}` : ''}</span>}
          </span>
          {/* T02：连续日读数（一次序列调用返回的逐日值）就近展开——日期/数值逐字来自服务端 */}
          {r.seriesText && <span className="kr-series">逐日：{r.seriesText}</span>}
          {changeCell(r)}
        </li>
      )
    }
    //  §1：段内超 `KR_READINGS_PER_OBJECT` 的读数在本商品段内就近展开（同一阅读单位）。
    const renderGroup = (group: FactGroupView, shown: FactRowView[]) => (
      <div className="kr-group" key={group.key}>
        <div className="kr-group-head">
          <span className="kr-name">{group.objectText}</span>
        </div>
        <ul className="kr-group-readings">{shown.map(renderReading)}</ul>
        {group.rows.length > shown.length && (
          <details className="kr-group-more">
            <summary>查看该商品其余 {group.rows.length - shown.length} 条读数</summary>
            <ul className="kr-group-readings">
              {group.rows.slice(shown.length).map(renderReading)}
            </ul>
          </details>
        )}
      </div>
    )
    return (
      <>
        <div className="kr-rows kr-rows-grouped">
          {render.groups.map(({ group, shown }) => renderGroup(group, shown))}
        </div>
        {render.overflowGroups > 0 && (
          <div className="kr-more">
            <p className="kr-more-count">
              共 {render.groups.length + render.hiddenGroups.length} 个商品，当前展示 {render.groups.length} 个
            </p>
            <details className="kr-more-list">
              <summary>查看全部 {render.hiddenGroups.length} 项（其余商品读数）</summary>
              <div className="kr-rows kr-rows-grouped">
                {render.hiddenGroups.map((group) => renderGroup(group, group.rows))}
              </div>
            </details>
          </div>
        )}
      </>
    )
  }

  const shown = render.flat
  const rest = render.rest
  //  §1：完整结果入口不按结果类型区分——所有平铺业务模块超限时都在本模块内展开。
  const expandableRows = rest.length > 0
  const named = rows
  const sameMetric = named.every((r) => r.metricText === named[0].metricText)
  const sameObject = named.every((r) => r.objectText === named[0].objectText)
  const head = shown[0]
  const countUnit = new Set(rows.map((r) => r.objectText)).size === rows.length ? '个商品' : '条数据'
  // /单期读数没有涨跌方向是事实形态，不是数据异常——不能据此再开一个口径展开入口。
  // 只有状态本身不是「正常读数」或读数缺失时，才需要逐行补一句状态说明与依据。
  const hasUsableReading = (r: FactRowView) => r.targetNumeric && (!r.hasPair || r.baseNumeric)
  const needsCaliberNote = (r: FactRowView) =>
    (Boolean(r.stateText) && r.stateText !== '正常读数') || !hasUsableReading(r)
  // 同组同口径只在这里说一次：块已声明周期时不再复述周期，只补取数方式。
  const sharedCaliber = periodDeclared
    ? (head.kindText ? `取数方式：${head.kindText}` : '')
    : [
        head.periodText !== '—' ? `周期：${head.periodText}` : '',
        head.kindText ? `取数方式：${head.kindText}` : '',
      ].filter(Boolean).join(' · ')
  const renderRow = (r: FactRowView) => {
    const name = sameMetric ? r.objectText
      : sameObject ? r.metricNameText
        : `${r.objectText} · ${r.metricNameText}`
    const unit = r.unitText ? ` ${r.unitText}` : ''
    // 只有成对事实才是「两期读数」；单期事实按自己的实际周期呈现——值里不再写死「本期」
    // （实际周期由本行的口径标注或组内统一口径说明给出），也不在变化栏伪造一个「—」。
    const values = r.hasPair
      ? `${r.baseNumText} → ${r.targetNumText}${unit}`
      : r.targetNumeric ? `${r.targetNumText}${unit}` : '—'
    const needDetail = needsCaliberNote(r)
    return (
      <div className="kr-row" key={r.key}>
        <div className="kr-name" title={r.periodText}>
          {name}
          {needDetail && (
            <details className="kr-row-details">
              <summary>查看依据与口径</summary>
              {/* 行内已就近给出周期与取数方式时（mixedCaliber），展开区不再复述同一事实 */}
              {!mixedCaliber && (
                <span>周期：{r.periodText} · 取数方式：{r.kindText} · </span>
              )}
              <span>状态：{r.stateText || (hasUsableReading(r) ? '正常读数' : '读数缺失，本次无法判定')}</span>
            </details>
          )}
        </div>
        <span className="kr-values">
          <ReadingCite handle={r.handle} text={values} cite={cite} />
          {mixedCaliber && (
            <span className="kr-caliber">{r.periodText}{r.kindText ? ` · ${r.kindText}` : ''}</span>
          )}
        </span>
        {/* T02：连续日读数就近展开（平铺形态同样可见，不只折叠在依据区） */}
        {r.seriesText && <span className="kr-series">逐日：{r.seriesText}</span>}
        {changeCell(r)}
      </div>
    )
  }
  return (
    <>
      <div className="kr-rows">{shown.map(renderRow)}</div>
      {expandableRows && (
        <div className="kr-more">
          <p className="kr-more-count">共 {rows.length} {countUnit}，当前展示 {FACT_ROW_LIMIT} 项</p>
          <details className="kr-more-list">
            <summary>查看剩余 {rest.length} 项</summary>
            <div className="kr-rows">{rest.map(renderRow)}</div>
          </details>
        </div>
      )}
      {!mixedCaliber && sharedCaliber ? (
        <p className="tb-note kr-shared-caliber">
          {sharedCaliber}
        </p>
      ) : null}
    </>
  )
}

function FactTable({ rows, cite }: { rows: FactRowView[]; cite?: CiteRender }) {
  return (
    <div className="table-scroll">
      <table className="compare-table tb-facts">
        <thead>
          <tr>
            <th>对象</th>
            <th>指标</th>
            <th>实际周期</th>
            <th>读数</th>
            <th>变化</th>
            <th>取数方式</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td className="cell-name">{r.objectText}</td>
              <td>{r.metricText}</td>
              <td>{r.periodText}</td>
              <td>
                <ReadingCite handle={r.handle} text={r.readingText} cite={cite} />
                {r.seriesText && <span className="kr-series">（逐日：{r.seriesText}）</span>}
              </td>
              <td>{r.changeText || '—'}</td>
              <td>{r.kindText}</td>
              <td>{r.stateText || '正常读数'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** 依据区（默认折叠）：全部事实的完整读数表（对象/周期/取数方式/状态/依据 chip）、
 *  复算集合（中位数成员与排除项）、排名、工具返回基准。展开后看到的是数据底稿。
 *  排除项与首屏**同一判据**（`firstScreenRender`）——首屏已逐条展示的行不再在依据区
 *  复述；声明对象名单只列首屏未出现的那些（完整名单与首屏重复的部分不再铺两遍）。 */
function Basis({ g, cite, shownHandles: shown }: {
  g: BlockGroupView
  cite?: CiteRender
  /** **跨组**首屏已出现的 handle 集合（依据区据此排除首屏已出现的行，不重复铺） */
  shownHandles: ReadonlySet<string>
}) {
  const hiddenRows = g.changeRows.filter((r) => !shown.has(r.handle)).length
  const firstScreenObjects = new Set(
    g.changeRows.filter((r) => shown.has(r.handle)).map((r) => r.objectText))
  for (const ranking of g.toolRankings) {
    for (const row of ranking.rows) firstScreenObjects.add(row.productText)
    for (const missing of ranking.missingGroup) firstScreenObjects.add(missing.productText)
  }
  const additionalFactRows = g.blocks.map((b) => ({
    block: b,
    rows: b.factRows.filter((r) => !shown.has(r.handle)),
  }))
  const remainingSubjects = g.blocks.map((b) => ({
    block: b,
    texts: b.subjectTexts.filter((t) => !firstScreenObjects.has(t)),
  }))
  const hasContent = hiddenRows > 0
    || additionalFactRows.some((x) => x.rows.length > 0)
    || remainingSubjects.some((x) => x.texts.length > 0)
    || g.blocks.some((b) => b.dataIssues.length > 0)
    || g.medians.length > 0 || g.ranks.length > 0 || g.toolMedians.length > 0
  if (!hasContent) return null
  return (
    <details className="tb-basis">
      <summary>数据依据（读数与筛选条件）</summary>

      {hiddenRows > 0 && (
        <p className="tb-note">
          另有 {hiddenRows} 条对象读数：
        </p>
      )}

      {additionalFactRows.some((x) => x.rows.length > 0) && (
        <div className="tb-basis-sec">
          <h4>读数（对象 · 周期 · 取数方式 · 状态）</h4>
          {additionalFactRows.map(({ block, rows }) => rows.length > 0 && (
            <FactTable key={block.key} rows={rows} cite={cite} />
          ))}
        </div>
      )}

      {remainingSubjects.some((x) => x.texts.length > 0) && (
        <div className="tb-basis-sec">
          <h4>其他分析对象</h4>
          {remainingSubjects.map(({ block, texts }) => texts.length > 0 && (
            <p className="tb-note" key={block.key}>
              分析对象 {texts.length} 个：{texts.join('、')}
            </p>
          ))}
        </div>
      )}

      {g.blocks.some((b) => b.dataIssues.length > 0) && (
        <div className="tb-basis-sec">
          <h4>数据说明</h4>
          {g.blocks.map((b) => b.dataIssues.length > 0 && (
            <ul className="tb-vlines" key={b.key}>
              {b.dataIssues.map((t, i) => <li key={`issue-${i}`}>{t}</li>)}
            </ul>
          ))}
        </div>
      )}

      {g.medians.length > 0 && (
        <div className="tb-basis-sec">
          <h4>中位数计算依据</h4>
          {g.medians.map((m) => (
            <div className="tb-median" key={m.key}>
              <p className="tb-median-head">
                {m.title}：中位数 <strong>{m.medianText}</strong>
                （{m.nText} 个成员{m.basisText ? ` · ${m.basisText}` : ''}）
              </p>
              <ul className="tb-members">
                {m.members.map((mem) => (
                  <li key={mem.key}>
                    <span>{mem.productText}</span>
                    <span className="tb-member-val">{mem.valueText}</span>
                  </li>
                ))}
              </ul>
              {m.excluded.length > 0 && (
                <ul className="tb-excluded">
                  {m.excluded.map((e) => (
                    <li key={e.key}>
                      {e.productText}：{e.stateText}（缺失不代 0，未参与计算）
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}

      {g.ranks.length > 0 && (
        <div className="tb-basis-sec">
          <h4>本次所选商品排名</h4>
          <ul className="tb-members">
            {g.ranks.map((r) => (
              <li key={r.key}>
                <span>{r.productText}</span>
                <span className="tb-member-val">
                  {r.valueText} · {r.rankText}
                  {r.basisText ? ` · ${r.basisText}` : ''}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {g.toolMedians.length > 0 && (
        <div className="tb-basis-sec">
          <h4>集合基准读数</h4>
          <ul className="tb-members">
            {g.toolMedians.map((t) => (
              <li key={t.key}>
                <span>
                  {t.label}
                  {`（${[
                    t.metricText,
                    t.periodText,
                    t.productIds.length ? `本次选中 ${t.productIds.length} 个商品` : '',
                  ].filter(Boolean).join(' · ')}）`}
                </span>
                <span className="tb-member-val">{t.medianText}
                  {(t.handle || t.ref) && <RefCite handle={t.handle ?? t.ref!} cite={cite} />}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </details>
  )
}

/** 单期集合合计视图（`kind='set_sum'` 引用；汇总与覆盖/缺失由服务端原样给出）。
 *  仅展示展示意图明确选中的真实合计引用——不因普通多商品 value 自动加卡，不从 subjects 拼总数。 */
function SinglePeriodTotalsSection({
  totals, cite,
}: { totals: SinglePeriodTotalsView[]; cite?: CiteRender }) {
  return (
    <div className="tb-single-period-totals">
      {totals.map((t) => (
        <div className="tb-single-total" key={t.key}>
          <p className="tb-single-total-head">
            <strong>单期集合合计 · {t.metricText}</strong>
            {(t.handle || t.ref) && cite
              ? <span className="tb-single-total-cite">{cite(t.handle ?? t.ref!)}</span>
              : null}
          </p>
          <p className="tb-single-total-meta">
            {t.timeText ? `时间 ${t.timeText}` : '时间 —'}
            {t.unitText ? ` · 单位 ${t.unitText}` : ''}
            {t.coverageText ? ` · ${t.coverageText}` : ''}
          </p>
          {t.nonComparableText && (
            <p className="tb-note tb-single-total-nc">{t.nonComparableText}</p>
          )}
          <p className="tb-single-total-value">
            {t.sumText ? <strong>{t.sumText}</strong> : '—'}
            <span className="tb-single-total-label"> · {t.label}</span>
          </p>
        </div>
      ))}
    </div>
  )
}

/**
 *  双品类并列首屏：把各块 `recomputed.group_totals`（服务端已复算）按**目录可信 category**
 * 分组并列——每个品类给 M1 基期→对比期合计、变化率与「可比/选中」覆盖；同品类下的 M2 等
 * 辅助指标随其后、各自披露独立覆盖。只渲染服务端已复算值：品类标签取可信 `category`；
 * 合计只代表块声明集合内两期均可比的商品，不代 0、不写「整个品类」结论。
 */
function GroupTotalsPanel({ totals, scope }: { totals: GroupTotalView[]; scope: TraceScope }) {
  const cats: string[] = []
  const byCat = new Map<string, GroupTotalView[]>()
  for (const t of totals) {
    const k = t.categoryTrusted ? t.categoryText : '未标注品类'
    if (!byCat.has(k)) { byCat.set(k, []); cats.push(k) }
    byCat.get(k)!.push(t)
  }
  const narrowed = totals.some((t) => t.nonComparableText)
  // 同一面板内所有分组共用同一比较周期时，周期只在顶部说明一次，不在每个商品组里重复。
  const periods = [...new Set(totals.map((t) => t.periodText).filter(Boolean))]
  const sharedPeriod = periods.length === 1 ? periods[0] : null
  return (
    <section className="tb-totals">
      <div className="tb-head">
        <span className="tb-kind">品类两期对比</span>
      </div>
      {(narrowed || sharedPeriod) && (
        <p className="tb-totals-scope">
          {sharedPeriod && `比较周期：${sharedPeriod}。`}
          {narrowed && '各指标按两期均可比商品合计；不可比项按状态单列，不按 0 计入。'}
        </p>
      )}
      {cats.map((cat) => (
        <div className="tb-totals-cat" key={cat} data-category={cat}>
          <p className="tb-totals-catname">{cat}</p>
          {[...byCat.get(cat)!.reduce((groups, t) => {
            const members = [...t.memberIds].sort()
            const key = members.length
              ? JSON.stringify([t.basePeriodId, t.targetPeriodId, members])
              : `unresolved:${t.key}`
            const existing = groups.get(key)
            if (existing) existing.push(t)
            else groups.set(key, [t])
            return groups
          }, new Map<string, GroupTotalView[]>()).entries()].map(([key, rows]) => {
            const first = rows[0]
            const selected = first.subjectsN ?? (first.memberIds.length ? first.memberIds.length : null)
            const knownNames = first.memberIds
              .map((id) => scope.products.find((p) => p.product_id === id)?.scope_label
                || scope.products.find((p) => p.product_id === id)?.product_name)
              .filter((name): name is string => Boolean(name))
            // 同组多指标覆盖/不可比范围一致时只写在组头一次，不再逐指标行重复。
            const commonCoverage = rows.length > 1 && rows.every((t) => t.coverageText === first.coverageText)
              ? first.coverageText : ''
            const commonNonComparable = rows.length > 1
              && rows.every((t) => t.nonComparableText === first.nonComparableText)
              ? first.nonComparableText : ''
            return (
              <div className="tb-total-set" key={key}>
                <p className="tb-total-set-head">
                  {first.periodText && first.periodText !== sharedPeriod ? `${first.periodText} · ` : ''}
                  {commonCoverage || (selected === null ? '本组商品范围未注明' : `本组所选 ${selected} 个商品`)}
                  {commonNonComparable ? ` · ${commonNonComparable}` : ''}
                </p>
                {knownNames.length > 0 && (
                  <details className="tb-total-set-members">
                    <summary>查看本组商品范围</summary>
                    <p>{knownNames.join('、')}</p>
                  </details>
                )}
                {rows.map((t) => {
                  const rowCoverage = [
                    t.coverageText !== commonCoverage ? t.coverageText : '',
                    t.nonComparableText && t.nonComparableText !== commonNonComparable ? t.nonComparableText : '',
                  ].filter(Boolean).join(' · ')
                  return (
                    <div className="tb-totals-row" key={t.key}>
                      <span className="tb-totals-metric">{t.metricText}</span>
                      <span className="tb-totals-sum">{t.sumText}</span>
                      <span className={`direction-value direction-${t.tone}`}>{t.changeText}</span>
                      {rowCoverage && <span className="tb-totals-cov">{rowCoverage}</span>}
                    </div>
                  )
                })}
              </div>
            )
          })}
        </div>
      ))}
      {narrowed && (
        <details className="tb-totals-detail">
          <summary>合计口径说明</summary>
          <p className="tb-note">
            可比范围可能因指标不同；合计不代表完整品类总量。
          </p>
        </details>
      )}
    </section>
  )
}

/**
 *  cohort_list 权威集合段：口径行（命中 N / M · 占比）＋不命中/无法判定分列＋周期行＋
 * 筛选条件＋更正横幅＋完整命中名单（可折叠、不截断，目录顺序非排名）＋无法判定成员＋证据入口。
 * 全部值来自视图模型（服务端权威段的确定性格式化），组件零计算。
 */
function CohortSection({ cv, cite, duplicate }: { cv: CohortView; cite?: CiteRender; duplicate?: boolean }) {
  if (duplicate) {
    return (
      <div className="tb-cohort tb-cohort--repeat">
        <p className="tb-note">
          补充依据
          <RefCite handle={cv.handle} cite={cite} />
        </p>
      </div>
    )
  }
  const full = cv.matches.length <= COHORT_INLINE_LIMIT
  const preview = full ? cv.matches : cv.matches.slice(0, COHORT_INLINE_LIMIT)
  const matchTable = (rows: typeof cv.matches) => (
    <div className="table-scroll">
      <table className="compare-table tb-cohort-table">
        <thead>
          <tr>
            <th>商品</th>
            {cv.conditionTexts.map((c) => <th key={c.key}>{c.text}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => (
            <tr key={m.key}>
              <td className="cell-name">{m.productText}</td>
              {m.readings.map((text, i) => (
                <td key={`${m.key}-${i}`}>{text}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
  return (
    <div className="tb-cohort">
      <p className="tb-cohort-count">
        {cv.countText} · {cv.shareText} <RefCite handle={cv.handle} cite={cite} />
      </p>
      {cv.classText && <p className="tb-cohort-classes">{cv.classText}</p>}
      {cv.periodText && cv.periodText !== '—' && (
        <p className="tb-cohort-period">{cv.periodText}</p>
      )}
      {cv.conditionTexts.length > 0 && (
        <>
          <p className="tb-cohort-conds-title">筛选条件</p>
          <ul className="tb-cohort-conds">
            {cv.conditionTexts.map((c) => <li key={c.key}>{c.text}</li>)}
          </ul>
        </>
      )}
      {cv.correctedNote && <p className="tb-banner" role="note">{cv.correctedNote}</p>}
      {cv.matches.length > 0 && (
        <>
          <p className="tb-cohort-matches-head">
            符合条件的商品（{cv.matches.length} 个，按目录顺序{full ? '' : `；先展示前 ${preview.length} 个`}）
          </p>
          <div className="tb-cohort-preview">{matchTable(preview)}</div>
          {!full && (
            <details className="tb-cohort-list">
              <summary>
                查看全部 {cv.matches.length} 个商品
              </summary>
              <div className="tb-cohort-full">{matchTable(cv.matches)}</div>
            </details>
          )}
        </>
      )}
      {cv.unknowns.length > 0 && (
        <details className="tb-cohort-unknowns">
          <summary>无法判定的商品（{cv.unknowns.length} 个，不计入命中）</summary>
          <ul className="tb-excluded">
            {cv.unknowns.map((u) => (
              <li key={u.key}>
                <span>{u.productText}</span>
                <span className="tb-member-val">{u.reasonsText}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

/**
 * 工具权威整体排名的数据区呈现（集合级）。名次/顺序/原值逐字来自服务端下发的 Data
 * `ranking`（`recomputed.rankings`），本组件零复算、不重排名、不按渲染序号重生成名次；
 * 成员范围与时间身份如实标注「本次选中集合」（刻意区别于全品类）；引用入口挂在集合标题行
 * （集合级轻量引用），不逐行新建入口；缺失项不挤成榜单尾名。
 */
function ToolRankingSection({ rankings, cite }: { rankings: ToolRankingView[]; cite?: CiteRender }) {
  const hasRelatedFacts = rankings.some((ranking) =>
    ranking.rows.some((row) => row.relatedFacts.length > 0))
  return (
    <div className="tb-rankings">
      {rankings.map((rk) => (
        <div className="tb-ranking" key={rk.key}>
          <p className="tb-ranking-head">
            <strong>排名 · {rk.title}</strong>
            {(rk.handle || rk.ref) && cite
              ? <span className="tb-ranking-cite">{cite(rk.handle ?? rk.ref!)}</span>
              : null}
          </p>
          {rk.scopeText && <p className="tb-ranking-scope">{rk.scopeText}</p>}
          {rk.rows.length > 0 ? (
            <table className="compare-table tb-ranking-table">
              <thead>
                <tr>
                  <th>名次</th><th>商品</th><th>数值</th>
                  {hasRelatedFacts && <th>同周期已查指标</th>}
                </tr>
              </thead>
              <tbody>
                {rk.rows.map((r) => (
                  <tr key={r.key} className={r.isMissing ? 'tb-ranking-missing' : undefined}>
                    <td>{r.rankText}</td>
                    <td>{r.productText}</td>
                    <td>{r.isMissing ? (r.missingText || '—') : r.valueText}</td>
                    {hasRelatedFacts && (
                      <td>
                        {r.relatedFacts.length > 0 ? (
                          <ul className="tb-ranking-related">
                            {r.relatedFacts.map((fact) => (
                              <li key={fact.key}>
                                <span className="tb-ranking-related-metric">{fact.metricNameText}</span>
                                <span>{fact.targetNumeric
                                  ? fact.targetNumText + (fact.unitText ? " " + fact.unitText : "")
                                  : '—'}</span>
                                <span className="tb-ranking-related-time">{fact.periodText}</span>
                                {fact.stateText && fact.stateText !== '正常读数'
                                  && <span className="tb-ranking-related-state">{fact.stateText}</span>}
                                <RefCite handle={fact.handle} cite={cite} />
                              </li>
                            ))}
                          </ul>
                        ) : '—'}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="tb-note tb-ranking-empty">本次没有可排名的读数。</p>
          )}
          {rk.missingGroup.length > 0 && (
            <ul className="tb-ranking-missing-list">
              {rk.missingGroup.map((member, i) => (
                <li key={String(member.productId ?? 'missing') + '-' + i}>
                  <span>{member.productText}</span>
                  <span>
                    {member.valueText}
                    {member.stateText ? ' · ' + member.stateText : ''}
                    {member.reasonText ? ' · ' + member.reasonText : ''}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {rk.missingGroup.length === 0 && rk.rows.length === 0 && (
            <p className="tb-ranking-empty-detail">本次未返回缺失商品明细。</p>
          )}
          {rk.collectionBaselines.map((baseline) => (
            <p className="tb-ranking-baseline" key={baseline.key}>
              <span>
                集合中位数 · {baseline.metricText || '指标本次未提供'} · {baseline.periodText || rk.title}
                {' · 与本榜同一集合的 '}{baseline.productIds.length}{' 个商品：'}
                <strong>{baseline.medianText}</strong>
              </span>
              {(baseline.handle || baseline.ref) && cite
                ? <span className="tb-ranking-cite">{cite(baseline.handle ?? baseline.ref!)}</span>
                : null}
            </p>
          ))}
        </div>
      ))}
    </div>
  )
}

/** 一组关键结果：一个区块外壳（一份依据区），组内逐块元信息行/数据区/局部说明。 */
function TypedGroup({ g, cite, dupCohorts, shownHandles }: {
  g: BlockGroupView
  cite?: CiteRender
  dupCohorts?: ReadonlySet<string>
  /** 跨组首屏已出现的 handle 集合（依据区排除用，见 Basis） */
  shownHandles: ReadonlySet<string>
}) {
  const splitCandidateBlocks = g.type === 'candidate_list' && g.blocks.length > 1
  const renderedBlocks = g.blocks.flatMap((block, blockIndex) => {
    const rows = rowsForBlock(g, block)
    const memberships = g.candidateMemberships.filter((membership) => membership.blockKey === block.key)
    const hasSurvivingBlockItem = (items: { key: string }[]) =>
      items.some((item) => item.key.startsWith(`${block.key}:`))
    const hasBlockContent = rows.length > 0 || memberships.length > 0 || Boolean(block.cohort)
      || block.dataIssues.length > 0 || block.auxRows.length > 0 || block.groupTotals.length > 0
      || block.medians.length > 0 || block.ranks.length > 0
      || hasSurvivingBlockItem(g.toolRankings) || hasSurvivingBlockItem(g.toolMedians)
      || hasSurvivingBlockItem(g.singlePeriodTotals)
    return hasBlockContent ? [{ block, blockIndex, rows, memberships }] : []
  })
  const metaPartsByBlock = renderedBlocks.map(({ block }) => block.cohort
    ? []
    : (block.metaText ?? '').split('\u3000').filter(Boolean))
  const commonMeta = renderedBlocks.length > 1
    ? metaPartsByBlock.slice(1).reduce((common, parts) =>
      common.filter((part) => parts.includes(part)), [...metaPartsByBlock[0]])
      .filter((part) => !part.startsWith('对象：'))
    : []
  const sharedMeta = new Set<string>()
  const metaByBlock = new Map(renderedBlocks.map(({ block }) => {
    const parts = (block.metaText ?? '').split('\u3000').filter(Boolean)
    const kept = parts.filter((part) => {
      if (splitCandidateBlocks && part.startsWith('对象：')) return false
      if (part.startsWith('对象：')) return true
      if (commonMeta.includes(part)) return false
      if (sharedMeta.has(part)) return false
      sharedMeta.add(part)
      return true
    })
    return [block.key, kept.join('　') || null] as const
  }))
  const commonMetaText = commonMeta.join('　') || null
  const groupDeclaredPeriod = declaredMetaPeriod(commonMetaText)
    ?? (renderedBlocks.length === 1
      ? declaredMetaPeriod(metaByBlock.get(renderedBlocks[0].block.key) ?? null)
      : null)
  return (
    <section className="tb-block tb-group" data-type={g.type}>
      <div className="tb-head">
        <span className="tb-kind">{g.kindLabel}</span>
      </div>
      {commonMetaText && <p className="tb-meta">{commonMetaText}</p>}

      {renderedBlocks.map(({ block: b, blockIndex, rows, memberships }) => {
        const metaText = metaByBlock.get(b.key) ?? null
        return (
          <div className="tb-unit" key={b.key}>
            {splitCandidateBlocks && (
              <p className="tb-note"><strong>第 {blockIndex + 1} 组候选商品
                {b.subjectIds.length > 0 ? `（${b.subjectIds.length} 个）` : ''}</strong></p>
            )}
            {/* cohort 块的 metaText 与权威集合段重复（命中分母/周期/条件已承载），不渲染。 */}
            {!b.cohort && metaText && <p className="tb-meta">{metaText}</p>}
            {memberships.map((membership) => (
              <div className="tb-candidate-membership" key={membership.blockKey}>
                <p className="tb-candidate-membership-head">候选商品（{membership.members.length} 个）</p>
                <ul className="tb-candidate-membership-list">
                  {membership.members.map((member) => (
                    <li key={member.key}>
                      <span>{member.productText}</span>
                      {member.handle && <RefCite handle={member.handle} cite={cite} />}
                    </li>
                  ))}
                </ul>
                <p className="tb-note">相关数据见「{membership.rankingTitle}」排名。</p>
              </div>
            ))}
            {b.cohort && <CohortSection cv={b.cohort} cite={cite} duplicate={dupCohorts?.has(b.key)} />}
            {b.dataIssues.length > 0 && (
              <div className="tb-issues">
                {b.dataIssues.map((t, i) => (
                  <p className="tb-banner" role="note" key={`issue-${i}`}>{t}</p>
                ))}
              </div>
            )}
            {/* 声明范围外的引用读数只作辅助回查——首屏只给一条说明，读数本身全量在依据区。
                措辞保持中性（不暴露「声明/合同」内部语言）：本层不区分该引用是合法背景、模型漏声明
                还是用途不明（内部诊断在服务端 identity_warnings/依据区），故不声称完成三类识别。 */}
            {b.auxRows.length > 0 && (
              <p className="tb-note tb-aux-note">
                另有 {b.auxRows.length} 条参考读数，见数据依据。
              </p>
            )}
            {splitCandidateBlocks && rows.length > 0 && <FactRows rows={rows} cite={cite}
              declaredPeriod={declaredMetaPeriod(metaText)} />}
          </div>
        )
      })}

      {g.toolRankings.length > 0 && <ToolRankingSection rankings={g.toolRankings} cite={cite} />}

      {g.singlePeriodTotals.length > 0 && (
        <SinglePeriodTotalsSection totals={g.singlePeriodTotals} cite={cite} />
      )}

      {!splitCandidateBlocks && g.changeRows.length > 0 && <FactRows rows={g.changeRows} cite={cite}
        declaredPeriod={groupDeclaredPeriod} />}

      <Basis g={g} cite={cite} shownHandles={shownHandles} />
    </section>
  )
}
