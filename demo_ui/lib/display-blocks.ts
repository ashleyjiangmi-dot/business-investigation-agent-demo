/**
 * 将服务端展示块映射为页面视图，只格式化已核验数据，不重算指标或补造事实。
 * 对象、指标和周期标签来自服务端目录；局部数据问题就近展示。
 * 按实际事实区分单期读数与两期变化，声明外引用只供回查。
 * 两期分组合计完整覆盖同一成员、指标、目标期与覆盖口径时，隐藏重复的单期合计展示。
 */
import type {
  BaselineSet,
  DisplayBlock,
  DisplayBlocks,
  DisplayCohort,
  DisplayFact,
  DisplayGroupTotal,
  FilterScope,
  QueryScope,
  WireScalar,
} from './trace-filter'
import { cohortConditionText, cohortReadingText, opLabel } from './evidence-view'
import { metricLabel, periodLabel, productName, type TraceScope } from './trace'

/* ---------- 确定性格式化（零重算：只把 wire 标量渲染成人读形态） ---------- */

function fmtPct(v: WireScalar): string {
  if (typeof v === 'number') return `${v.toFixed(2)}%`   // 展示层两位（不改数、不复算）
  if (v === null || v === undefined) return ''
  return cohortReadingText(v)
}

const DIR_ZH: Record<string, string> = { up: '上升', down: '下降', flat: '持平' }

const BASIS_ZH: Record<string, string> = {

  server_recomputed: '',
  tool_returned: '',
}

function basisLabel(basis: string | null): string {
  if (!basis) return ''
  return BASIS_ZH[basis] ?? basis   // 未收录 token 原样给（不硬译）
}

function objText(pid: string | null, scope: TraceScope): string {
  if (!pid) return '—'
  return productName(pid, scope)
}

function metricText(m: string | null, scope: TraceScope): string {
  if (!m) return '—'
  return metricLabel(m, scope)
}

/** 值 + 单位后缀（数值千分位；状态文案不加单位；null='—'）。 */
function withUnitNum(v: WireScalar, unitSuffix: string | null): string {
  if (v === null || v === undefined) return '—'
  const text = cohortReadingText(v)
  if (typeof v === 'string' && text !== v) return text
  return unitSuffix ? `${text}${unitSuffix}` : text
}

/** 首屏行名用无代号指标名（scope.metrics 直查；缺失回退代号本身——不猜名字，代号仍进依据区）。 */
function metricName(m: string | null, scope: TraceScope): string {
  if (!m) return '—'
  return scope.metrics?.[m] ?? m
}

/* ---------- 行级视图 ---------- */

export type FactRowView = {
  key: string
  /** 本块事实 handle（cite chip 的定位键；E 编号只作定位用途，不进可见文案） */
  handle: string
  ref: string
  /** 只用于和本展示组内的集合排名按真实身份关联；不取展示名反向匹配。 */
  productId: string | null
  metric: string | null
  periodId: string | null
  timeKey: string | null
  objectText: string
  metricText: string
  /** 实际周期：单期=periodLabel；两期=base → target */
  periodText: string
  /** 读数：单值「8,100 单」或两期「4,500 → 1,300 单」（单位取 fact.unit） */
  readingText: string
  /** 变化：「-3,200 单（-71.11%）· 下降」；单期事实为空串 */
  changeText: string
  kindText: string
  stateText: string
  /** base/target 两期成对＝指标变化卡可用 */
  hasPair: boolean
  baseLabel: string
  baseText: string
  targetLabel: string
  targetText: string
  directionText: string
  /** ——  首屏行式渲染字段（全部确定性派生：千分位/两位百分比/词表，零复算零改写）—— */
  /** 无单位千分位读数（unknown(...) 状态串原样；单期事实 targetNumText=value） */
  baseNumText: string
  targetNumText: string
  /** base/target 值是有限数字（unknown 串/缺失=false，首屏行按 unknown 形态渲染） */
  baseNumeric: boolean
  targetNumeric: boolean
  unitText: string
  /** 变化幅度（两位百分比）；percent 缺失=空串（行内给「—」） */
  pctText: string
  /** 变化值色档：升/降/持平来自工具 direction；state 非 ok 或方向缺失=unknown（灰） */
  tone: 'up' | 'down' | 'flat' | 'unknown'
  /** 无代号指标名（首屏行名用；scope.metrics 直查，缺失回退代号——不猜名字） */
  metricNameText: string
  /** 日期窗口事实的真实起止日期文本（`2026-05-27~2026-05-27`）；非窗口事实=空串。
   *  窗口事实的 `periodText` 已由 `periodLabel` 目录外原样显示该日期区间，此字段供逐日序列头使用。 */
  windowText: string
  /** 逐日读数序列（`day_series` 事实专用；其他事实=空数组）。日期/value/state 逐字来自 wire。 */
  series: { key: string; dateText: string; valueText: string; stateText: string }[]
  /** 逐日序列的一行文本（`05-21 520 · 05-22 533 · …`）；无序列=空串 */
  seriesText: string
  /** 本条读数是否落在块声明范围内——声明内＝本块主要展示数据；声明外＝只作辅助回查，
   *  不上首屏（用途不明时不强塞入表， §9/§13）。判据＝指标/周期，另按块型语义叠加
   *  对象归属（`subjects` 即待展示商品的块型；`comparison` 不叠对象过滤）。 */
  declaredScope: boolean
}

export type MedianGroupView = {
  key: string
  title: string
  medianText: string
  nText: string
  basisText: string
  members: { key: string; productText: string; valueText: string }[]
  excluded: { key: string; productText: string; stateText: string }[]
}

export type RankRowView = {
  key: string
  productText: string
  valueText: string
  rankText: string
  basisText: string
}

export type ToolRankingMissingView = {
  productId: string | null
  productText: string
  valueText: string
  stateText: string
  reasonText: string
}

export type ToolMedianView = {
  key: string
  label: string
  metric: string | null
  metricText: string
  periodId: string | null
  periodText: string
  productIds: string[]
  unitText: string
  medianText: string
  handle: string | null
  ref: string | null
}

/** 单期集合合计视图（`kind='set_sum'` 引用；数值/覆盖/缺失均原样取自 Data，不做重算） */
export type SinglePeriodTotalsView = {
  key: string
  metric: string | null
  metricText: string
  unitText: string
  sumText: string
  /** 「本次集合完整合计」/「已知可用值合计（不含缺失）」/「unknown」 */
  label: string
  timeText: string
  /** Data 原时间身份（period_id 或窗口起止），仅用于去重，不做跨模式推断。 */
  timeIdentity: string | null
  coverageText: string
  nonComparableText: string
  /** 原始集合成员用于实际身份去重；完整成员/覆盖可从 set_sum 详情核对。 */
  productIds: string[]
  handle: string | null
  ref: string | null
  /** ——  跨类型覆盖判定用内部字段（wire 原值；不进可见文案，零复算）—— */
  selectedN: number | null
  comparableN: number | null
  nonComparableN: number | null
  nonComparableByState: Record<string, number>
  sumScope: string | null
  /** Data 原始合计（数值或 unknown(...) 状态串）；仅用于覆盖佐证，不改写。 */
  sumValue: WireScalar
}

/** 工具权威整体排名视图（集合级；名次/顺序/原值逐字来自 Data `ranking`，零复算）。 */
export type ToolRankingView = {
  key: string
  /** 榜单范围与时间标题（指标 · 周期/日期窗口） */
  title: string
  metric: string | null
  periodId: string | null
  productIds: string[]
  unitText: string
  /** 本次选中集合的成员范围与可用/缺失（Data 计数；缺失项不挤成榜单尾名） */
  scopeText: string
  /** 引用入口（集合级轻量引用；缺省=不出入口） */
  ref: string | null
  handle: string | null
  missingGroup: ToolRankingMissingView[]
  collectionBaselines: ToolMedianView[]
  rows: {
    key: string
    productId: string | null
    rankText: string
    /** 原名次数字（非数值/缺失=null；页面不按渲染序号重生成名次） */
    rankNum: number | null
    productText: string
    valueText: string
    isMissing: boolean
    missingText: string
    relatedFacts: FactRowView[]
  }[]
}

/* ---------- 本轮实际调查范围摘要（只取实际调查投影与事实，不铺可查询能力） ---------- */

/**
 * 结果区轻量调查范围：只回答「这次实际查了什么」——实际调查对象、实际分析周期，以及
 * **会改变业务解读**的指标范围/比较基准/筛选范围差异。
 *
 * 纪律：对象与周期只取 `query_scope`（本轮实际请求）与已核验事实自身的周期文本；**不用**
 * 入口允许周期等可查询范围顶替实际周期，也不展示可查询池/成功查询次数。缺键
 * （旧 SSE/无成功查询）＝不渲染摘要。
 */
export type InvestigationScopeRow = { key: string; label: string; text: string }
export type InvestigationScopeView = { rows: InvestigationScopeRow[] }

/** 引用详情已经给出的真实周期（`periodId` 是同一周期的去重身份；`text` 是展示文本）。 */
export type ActualPeriodRef = { periodId: string | null; text: string }

/**
 * 实际分析周期：合并**两类真实来源**——已核验事实行（展示块）与引用详情登记的周期。
 * 只取其中一个来源会漏掉其他查询实际用过的周期；两处都取、按周期身份去重后按日期先后输出。
 * 块内只给目录标签/原 ID 时，用引用详情的同一 `periodId` 归并到日期文本，不重复铺同一周期。
 * 只使用实际查询周期。
 */
export function collectActualPeriods(
  views: BlockView[],
  evidencePeriods: ActualPeriodRef[] = [],
): string[] {
  const byIdentity = new Map<string, string>()
  const texts = new Set<string>()
  const keep = (key: string, text: string) => {
    if (!text || texts.has(text)) return
    if (!byIdentity.has(key)) byIdentity.set(key, text)
    texts.add(text)
  }
  const detailById = new Map<string, ActualPeriodRef>()
  const detailByText = new Map<string, ActualPeriodRef>()
  for (const ref of evidencePeriods) {
    const text = ref.text.trim()
    if (!text) continue
    if (ref.periodId) {
      detailById.set(ref.periodId, ref)
      keep(`id:${ref.periodId}`, text)
    } else {
      keep(`text:${text}`, text)
    }
    detailByText.set(text, ref)
  }
  for (const view of views) {
    for (const row of [...view.factRows, ...view.changeRows]) {
      const text = row.periodText
      if (!text || text === '—') continue
      for (const part of text.split(' → ')) {
        const label = part.trim()
        if (!label || label === '—') continue
        // 详情同一周期（目录 ID 或展示文本命中）→ 归并到详情文本（通常是真实日期）
        const matched = detailById.get(label) ?? detailByText.get(label)
        if (matched) {
          keep(matched.periodId ? `id:${matched.periodId}` : `text:${matched.text}`, matched.text)
          continue
        }
        keep(`text:${label}`, label)
      }
    }
  }
  return [...byIdentity.values()].sort((a, b) => (a.match(/20\d{2}-\d{2}-\d{2}/)?.[0] ?? a)
    .localeCompare(b.match(/20\d{2}-\d{2}-\d{2}/)?.[0] ?? b))
}

function countText(n: number): string {
  return n.toLocaleString('zh-CN')
}

/** scope_kind → 业务话术；未收录 token 原样给（不硬译、不猜语义）。 */
function scopeKindText(kind: string | null, n: number | null): string {
  const nText = n === null ? '成员数未给出' : `${countText(n)} 个商品`
  if (kind === 'catalog' || kind === 'benchmark_all') return `目录全池（${nText}）`
  if (kind === 'category') return `指定品类（${nText}）`
  if (kind === 'single_product') return `指定对象（${nText}）`
  if (kind === 'product_ids') return `指定商品集合（${nText}）`
  return nText
}

function baselineText(b: BaselineSet, scope: TraceScope): string {
  const metric = b.metric ? metricName(b.metric, scope) : '未标注指标'
  const nText = b.product_n === null ? '成员数未给出' : `${countText(b.product_n)} 个成员`
  const periods = b.period_ids.map((p) => periodLabel(p, scope)).filter(Boolean)
  return `${metric} · ${nText}${periods.length ? ` · ${periods.join(' → ')}` : ''}`
}

function filterText(f: FilterScope): string {
  return scopeKindText(f.scope_kind, f.product_n)
}

export function buildInvestigationScopeView(
  query: QueryScope | null,
  actualPeriods: string[],
  scope: TraceScope,
): InvestigationScopeView | null {
  if (!query) return null
  const rows: InvestigationScopeRow[] = []
  const filterTexts = [...new Set((query.filter_scopes ?? []).map(filterText))]
  // 对象范围：有直接请求成员时给实际查询数；纯 cohort 调查（成员并集为 0）用筛选范围说话，
  // 不铺误导性的「0 个商品」。
  if (query.investigation_product_n > 0) {
    rows.push({
      key: 'objects',
      label: '调查对象',
      text: `实际查询 ${countText(query.investigation_product_n)} 个商品`,
    })
  } else if (filterTexts.length) {
    rows.push({ key: 'objects', label: '调查对象', text: filterTexts.join('、') })
  }
  if (actualPeriods.length) {
    rows.push({ key: 'periods', label: '分析周期', text: actualPeriods.join('、') })
  }
  // 指标→成员集合：是否同范围按成员身份判定；数量相同但成员不同仍须披露。
  const perMetric = Object.entries(query.per_metric)
  const memberSets = perMetric.map(([, ids]) => [...new Set(ids)].sort())
  const firstMembers = memberSets[0] ?? []
  const perMetricDifferent = memberSets.length > 1 && memberSets.some((ids) =>
    ids.length !== firstMembers.length || ids.some((id, i) => id !== firstMembers[i]))
  if (perMetricDifferent) {
    rows.push({
      key: 'permetric',
      label: '指标范围',
      text: perMetric
        .map(([m], i) => `${m ? metricName(m, scope) : '未标注指标'} ${countText(memberSets[i].length)} 个`)
        .join(' · '),
    })
  }
  const baselineTexts = (query.baseline_sets ?? []).map((b) => baselineText(b, scope))
  if (baselineTexts.length) {
    rows.push({ key: 'baseline', label: '比较基准', text: baselineTexts.join('；') })
  }
  if (query.investigation_product_n > 0 && filterTexts.length) {
    rows.push({ key: 'filter', label: '筛选范围', text: filterTexts.join('、') })
  }
  return rows.length ? { rows } : null
}

/* ---------- cohort_list 权威集合视图（全部值来自服务端已绑定的权威段，零复算） ---------- */

export type CohortConditionView = { key: string; text: string }

export type CohortMatchView = {
  key: string
  productText: string
  /** 与条件列对齐的读数（数值千分位；unknown 状态串原样） */
  readings: string[]
}

export type CohortUnknownView = { key: string; productText: string; reasonsText: string }

export type CohortView = {
  key: string
  /** 证据 chip 定位键（服务端集合引用的 handle） */
  handle: string
  /** 权威集合等价签名（呈现层重复识别用；取服务端权威段字段，
   *  含范围身份；范围身份与服务端引用都缺失时以块身份占位＝不推定等价） */
  signature: string
  binding: string | null
  scopeText: string
  periodText: string
  conditionTexts: CohortConditionView[]
  /** 口径行：命中 N / M（分母＝筛选范围；范围名与期间不再另铺，已被命中分母/周期行覆盖） */
  countText: string
  /** 占比按**显式分母口径**呈现——「本轮范围内已确认命中占比 P%」；分母内全部命中＝「本轮范围内全部命中」
   *  （P 缺失＝服务端未给占比，照原样留空位，不代 0）。 */
  shareText: string
  /** 命中之外的另两类成员计数分列（不命中 / 无法判定）；两类都为 0＝null（不铺「不命中 0」）。
   *  无法判定不计入命中；不命中只表示本轮范围内未满足筛选条件，不是「可判定总数」。 */
  classText: string | null
  zeroHit: boolean
  matches: CohortMatchView[]
  unknowns: CohortUnknownView[]
  /** corrected：模型名单/计数与权威不一致的结构化更正说明；authority=无 */
  correctedNote: string | null
}

function buildCohortView(c: DisplayCohort, scope: TraceScope, blockKey: string): CohortView {
  const unknownCount = c.predicate_unknown_count ?? 0
  const matchCount = c.match_count ?? 0
  const scopeCount = c.scope_count ?? 0
  const pct = typeof c.confirmed_share_percent === 'number' ? c.confirmed_share_percent.toFixed(2) : null
  // 占比按**显式分母口径**呈现（ 的「占比直接跟随分母行」保留，但文案点明分母＝本轮
  // 筛选范围，且不把 unknown 并进同一括号——三类成员改在下一行分列；分母内全部命中＝「全部命中」）。
  const shareText = unknownCount === 0 && matchCount === scopeCount
    ? '本轮范围内全部命中'
    : `本轮范围内已确认命中占比 ${pct ?? '—'}%`
  // 命中之外的另两类成员分列（命中在 countText 的分母行里）。两类都为空/为 0＝null，
  // 不铺「不命中 0」这行零信息文案；无法判定明确标注不计入命中。
  const classBits: string[] = []
  if (typeof c.nonmatch_count === 'number' && c.nonmatch_count > 0) {
    classBits.push(`不命中 ${c.nonmatch_count.toLocaleString('zh-CN')}`)
  }
  if (unknownCount > 0) {
    classBits.push(`无法判定 ${unknownCount.toLocaleString('zh-CN')}（不计入命中）`)
  }
  const classText = classBits.length ? classBits.join(' · ') : null
  const periodText = c.time_mode === 'single_period'
    ? periodLabel(c.period_id, scope) || '—'
    : `${periodLabel(c.base_period_id, scope) || '—'} → ${periodLabel(c.target_period_id, scope) || '—'}`
  const rec = c.reconciliation
  const correctedBits: string[] = []
  if (rec) {
    if (rec.missing_ids.length) correctedBits.push(`漏列 ${rec.missing_ids.length} 个`)
    if (rec.extra_ids.length) correctedBits.push(`多列 ${rec.extra_ids.length} 个`)
    if (rec.duplicate_ids.length) correctedBits.push(`重复 ${rec.duplicate_ids.length} 个`)
    if (rec.match_count_declared !== null && rec.match_count_declared !== rec.match_count_actual) {
      correctedBits.push(`命中数声明 ${rec.match_count_declared} 与实际 ${rec.match_count_actual} 不符`)
    }
    if (rec.scope_count_declared !== null && rec.scope_count_declared !== rec.scope_count_actual) {
      correctedBits.push(`范围数声明 ${rec.scope_count_declared} 与实际 ${rec.scope_count_actual} 不符`)
    }
  }
  return {
    key: `${blockKey}-cohort`,
    handle: c.handle ?? '',
    signature: cohortSignature(c, blockKey),
    binding: c.binding,
    scopeText: c.scope_kind === 'product_ids'
      ? `指定商品集合（${scopeCount} 个）`
      : `全池（${scopeCount} 个商品）`,
    periodText,
    conditionTexts: c.conditions.map((cond, i) => ({
      key: `cond-${i}`,
      text: cohortConditionText(cond, scope),
    })),
    countText: `命中 ${matchCount.toLocaleString('zh-CN')} / ${scopeCount.toLocaleString('zh-CN')}`,
    shareText,
    classText,
    zeroHit: matchCount === 0,
    matches: c.match_rows.map((r, i) => ({
      key: `row-${i}-${r.product_id ?? ''}`,
      productText: objText(r.product_id, scope),
      readings: r.readings.map((rd) => cohortReadingText(rd.value)),
    })),
    unknowns: c.unknown_members.map((u, i) => ({
      key: `unk-${i}-${u.product_id ?? ''}`,
      productText: objText(u.product_id, scope),
      reasonsText: u.reasons.map((r) => cohortReadingText(r)).join('；') || '原因未登记',
    })),
    correctedNote: rec?.status === 'corrected' && correctedBits.length
      ? `名单或数量与实际筛选结果不一致（${correctedBits.join('、')}），已按核实结果更正；以下为完整名单。`
      : null,
  }
}

/* ---------- comparison 块的分组合计视图（只渲染服务端已复算的 recomputed.group_totals） ---------- */

/** `percent_change_state` 非 ok 时的口径说明（服务端状态码 → 业务话术；未收录原样给）。 */
const PCT_STATE_ZH: Record<string, string> = {
  zero_denominator: '基期合计为 0，未给比率（不按 0% 处理）',
  no_comparable_products: '无可比商品，未给比率',
}

export type GroupTotalView = {
  key: string
  /** 展示去重等价键（不外显）：来源范围（块声明对象集）＋品类＋指标＋两期＋全部复算事实。
   *  仅用于跨块展示去重；不同商品子集即使合计恰好相同也必不同键（含成员集，不只比结果）。 */
  dedupeKey: string
  /** 目录可信品类（服务端 `category` 字段；与 Data 侧同一目录单点，非模型叙述文字） */
  categoryText: string
  /** 品类标签是否为目录可信字段（false=服务端未给品类，按「未标注品类」中性呈现） */
  categoryTrusted: boolean
  /** 指标代号（排序用：M1 订单量为主读数排前，M2 辅助观察随后——计划口径，非按品类硬编码） */
  metric: string | null
  metricText: string
  periodText: string
  /** 两期合计「75,200 → 63,300 单」 */
  sumText: string
  /** 变化「-11,900 单（-15.82%）」；比率缺失时给状态说明 */
  changeText: string
  /** 变化值色档：只按服务端复算的变化量符号着色，不代表业务好坏 */
  tone: 'up' | 'down' | 'flat' | 'unknown'
  /** 覆盖「可比 12 / 选中 12」 */
  coverageText: string
  /** 不可比披露「2 个不可比（身份跨期不一致 1、对比期无记录 1）」；全可比时为空串 */
  nonComparableText: string
  /** ——  跨类型覆盖判定用内部字段（wire 原值；不进可见文案，零复算）—— */
  /** 本组合计的真实成员集：块声明对象集（服务端复算输入面）按目录品类过滤推导；
   *  品类不可信（未标注）时＝全部声明成员；声明集为空或过滤后为空＝无法证实（[]）。 */
  memberIds: string[]
  basePeriodId: string | null
  targetPeriodId: string | null
  subjectsN: number | null
  comparableN: number | null
  nonComparableN: number | null
  nonComparableByState: Record<string, number>
  sumScope: string | null
  /** Data 原始目标期合计（数值或 unknown(...) 状态串）；仅用于覆盖佐证，不改写。 */
  targetSumValue: WireScalar
}

function totalTone(percent: WireScalar, absolute: WireScalar, state: string | null): GroupTotalView['tone'] {
  if (state !== 'ok') return 'unknown'
  const n = typeof percent === 'number' ? percent : typeof absolute === 'number' ? absolute : null
  if (n === null) return 'unknown'
  return n > 0 ? 'up' : n < 0 ? 'down' : 'flat'
}

/**
 * 分组合计展示去重等价键：**来源范围＋指标＋周期＋实际计算事实**全等才算同一份汇总。
 * 只比品类名/指标/结果是不够的——不同商品子集（如 {A,B} 与 {C,D}）合计恰好相同也必须分别保留，
 * 故把块声明对象集（服务端复算的输入范围单点）排序后纳入键。来源范围不可识别时（旧/畸形输入
 * 缺 display.subjects）保守退化为块身份，永不与他块合并（宁可重复展示，不误删合法结果）。
 */
function groupTotalDedupeKey(t: DisplayGroupTotal, blockKey: string, i: number, scopeKey: string): string {
  if (!scopeKey) return `${blockKey}-tot-${i}`
  return JSON.stringify([
    scopeKey, t.category, t.metric, t.unit,
    t.base_period_id, t.target_period_id,
    t.subjects_n, t.comparable_n, t.non_comparable_n, t.non_comparable_by_state,
    t.base_sum, t.target_sum, t.absolute_change, t.percent_change,
    t.percent_change_state, t.sum_scope,
  ])
}

/**
 * 分组合计的真实成员集推导（跨类型覆盖判定的成员身份单点）。
 *
 * 分组合计的输入面＝块声明对象集（`display.subjects`，服务端 `_recompute_group_totals`
 * 的同一输入）；按目录品类拆分时，各组真实成员＝声明集内该品类的商品（目录单点
 * `scope.products[].category`，与服务端 `_category_totals` 分组口径同一来源）。
 * 品类不可信（服务端未给 `category`）时该组即全部声明成员。声明集为空、或过滤后为空
 * （目录缺成员/品类不符）＝**无法证实**，返回 `[]`——调用方按「不可证」保守保留单期项。
 * 不传入 scope（旧调用/夹具）时退化为「声明集即成员集」：仍可按真实商品 ID 集合证伪，
 * 但不做跨品类切分（宁可不隐藏，不误删合法单期结果）。
 */
function groupTotalMemberIds(t: DisplayGroupTotal, declaredSubjects: string[],
                             scope?: TraceScope): string[] {
  if (!declaredSubjects.length) return []
  const trusted = typeof t.category === 'string' && t.category.trim() !== ''
  if (!trusted || !scope || !scope.products.length) return [...declaredSubjects]
  const categoryOf = new Map(scope.products.map((p) => [p.product_id, p.category]))
  const members = declaredSubjects.filter((id) => categoryOf.get(id) === t.category)
  return members.length ? members : []
}

function buildGroupTotalView(t: DisplayGroupTotal, i: number, scope: TraceScope, blockKey: string,
                             scopeKey: string, declaredSubjects: string[]): GroupTotalView {
  const unit = t.unit ? ` ${t.unit}` : ''
  const state = t.percent_change_state
  const pctNum = typeof t.percent_change === 'number' ? t.percent_change : null
  const absNum = typeof t.absolute_change === 'number' ? t.absolute_change : null
  const absBits = absNum !== null
    ? `${cohortReadingText(absNum)}${unit}`
    : typeof t.absolute_change === 'string' ? withUnitNum(t.absolute_change, unit) : ''
  const pctBits = pctNum !== null ? `${pctNum.toFixed(2)}%` : ''
  // 比率缺失时把**口径状态**显形（不写 0%、不写破折号假数）：服务端状态码优先，
  // 其次原样给服务端的 unknown(...) 串——两者都不进 UI 自行判断
  const stateNote = state && state !== 'ok'
    ? (PCT_STATE_ZH[state] ?? cohortReadingText(state))
    : (pctNum === null && typeof t.percent_change === 'string' ? fmtPct(t.percent_change) : '')
  const changeText = [
    absBits ? `${absBits}${pctBits ? `（${pctBits}）` : ''}` : '',
    pctBits ? '' : stateNote,
  ].filter(Boolean).join(' · ') || '—'
  const comparable = t.comparable_n
  const subjects = t.subjects_n
  const coverageText = comparable === null && subjects === null
    ? ''
    : `可比 ${comparable ?? '—'} / 选中 ${subjects ?? '—'}`
  const byState = Object.entries(t.non_comparable_by_state ?? {})
  const nonComparableText = (t.non_comparable_n ?? 0) > 0
    ? `${t.non_comparable_n} 个不可比${byState.length ? `（${byState.map(([s, n]) => `${cohortReadingText(s)} ${n}`).join('、')}）` : ''}`
    : ''
  const periodText = t.base_period_id && t.target_period_id
    ? `${periodLabel(t.base_period_id, scope)} → ${periodLabel(t.target_period_id, scope)}`
    : ''
  return {
    key: `${blockKey}-tot-${i}`,
    dedupeKey: groupTotalDedupeKey(t, blockKey, i, scopeKey),
    categoryText: t.category ?? '未标注品类',
    categoryTrusted: t.category !== null && t.category !== '',
    metric: t.metric,
    metricText: metricText(t.metric, scope),
    periodText,
    sumText: `${withUnitNum(t.base_sum, unit)} → ${withUnitNum(t.target_sum, unit)}`,
    changeText,
    tone: totalTone(t.percent_change, t.absolute_change, state),
    coverageText,
    nonComparableText,
    memberIds: groupTotalMemberIds(t, declaredSubjects, scope),
    basePeriodId: t.base_period_id,
    targetPeriodId: t.target_period_id,
    subjectsN: t.subjects_n,
    comparableN: t.comparable_n,
    nonComparableN: t.non_comparable_n,
    nonComparableByState: t.non_comparable_by_state ?? {},
    sumScope: t.sum_scope,
    targetSumValue: t.target_sum,
  }
}

/* ---------- 首屏呈现阈值 + 分组合计收集 ---------- */

/** 首屏全露上限：≤ 此数的权威名单默认全露；超过则先露前若干项＋「查看全部 N 项」。 */
export const COHORT_INLINE_LIMIT = 6
/** 首屏关键读数行上限：超过则首屏只留前若干条； §1：剩余项由业务模块内的
 *  「查看剩余 N 项」展开完整结果（不再只交给依据区），完整读数行同时仍在核查依据原始读数表。
 *  （：仅用于「每行一个对象」的平铺形态；商品为段的形态按 `KR_GROUP_LIMIT` 计商品数。） */
export const FACT_ROW_LIMIT = 8
/** 商品是经营阅读的第一视觉单位——首屏最多直接铺出的**商品**数（按商品分段）。
 *   §1：超限商品段由业务模块内的「查看全部 N 项」展开（不只交给依据区）。 */
export const KR_GROUP_LIMIT = 8
/** 单个商品在首屏最多直接铺出的读线条数； §1：其余读数由该商品段内的
 *  「查看该商品其余 N 条读数」就近展开（不丢数据，依据区完整读数表仍在）。 */
export const KR_READINGS_PER_OBJECT = 3

export type FactGroupView = {
  key: string
  objectText: string
  rows: FactRowView[]
}

/**
 *  §4：形状合法但**没有任何可呈现业务结果**的空壳块判据。
 *
 * 空壳不展示（不猜内容、不补数据、不造结论）；移除空壳后没有有效业务块时，由消费链
 * （`app/page.tsx`）整份降级到已有完整规范正文。合法零命中/未知 cohort、合法空榜
 * （ §7.2：带范围/时间/缺失明细）都仍是有效块——它们有可呈现的结果形态，
 * **不按行数为 0 或缺数值判无效**。纯 `data_issues`（服务端问题局部说明）本身不是
 * 业务结果：只带说明、没有任何数据的块按空壳处理（原文仍在 raw wire 可回查）。
 */
export function hasPresentableResult(v: BlockView): boolean {
  return Boolean(v.cohort)
    || v.changeRows.length > 0
    || v.groupTotals.length > 0
    || v.medians.length > 0
    || v.ranks.length > 0
    || v.toolRankings.length > 0
    || v.toolMedians.length > 0
    || v.singlePeriodTotals.length > 0
}

/** 首屏关键读数按商品分组（首见顺序保持：同一商品的行聚为一段）。零复算，只做确定性归并。
 *   §3：按**对象身份**（`product_id`）归组，不按显示名猜 ID——同名不同 ID 的两个商品
 *  不得并成同一阅读段；ID 缺失（集合级事实）才退回显示名（原行为）。 */
export function groupFactRowsByObject(rows: FactRowView[]): FactGroupView[] {
  const out: FactGroupView[] = []
  const index = new Map<string, FactGroupView>()
  for (const r of rows) {
    const key = r.productId ?? `name:${r.objectText}`
    let g = index.get(key)
    if (!g) {
      g = { key, objectText: r.objectText, rows: [] }
      index.set(key, g)
      out.push(g)
    }
    g.rows.push(r)
  }
  return out
}

/** FactRows 与 Basis 共用的**唯一**首屏判据——依据区据此排除首屏已出现的行，避免同一事实铺两遍。 */
export type FirstScreenRender = {
  /** true＝商品为第一视觉单位（一个商品一段）；false＝既有平铺行（每行一对象/读数） */
  grouped: boolean
  groups: { group: FactGroupView; shown: FactRowView[]; hiddenCount: number }[]
  /**  §1：超 KR_GROUP_LIMIT 未默认展开的**商品段**（完整读数仍全量在依据区）；
   *  业务模块的「查看全部 N 项」据此在同一阅读单位内展开完整结果——剩余项不只交给 Basis。 */
  hiddenGroups: FactGroupView[]
  /** 超首屏上限、未上首屏的商品数（其余读数仍全量在依据区） */
  overflowGroups: number
  flat: FactRowView[]
  rest: FactRowView[]
  /** 首屏实际出现的 handle（依据区排除用） */
  shownHandles: Set<string>
}

export function firstScreenRender(rows: FactRowView[]): FirstScreenRender {
  const groups = groupFactRowsByObject(rows)
  const grouped = groups.length > 1 && groups.some((g) => g.rows.length > 1)
  const shownHandles = new Set<string>()
  if (grouped) {
    const shownGroups = groups.slice(0, KR_GROUP_LIMIT)
    const hiddenGroups = groups.slice(KR_GROUP_LIMIT)
    const out = shownGroups.map((group) => {
      const shown = group.rows.slice(0, KR_READINGS_PER_OBJECT)
      for (const r of shown) shownHandles.add(r.handle)
      return { group, shown, hiddenCount: group.rows.length - shown.length }
    })
    return {
      grouped: true, groups: out, hiddenGroups, overflowGroups: hiddenGroups.length,
      flat: [], rest: [], shownHandles,
    }
  }
  const flat = rows.slice(0, FACT_ROW_LIMIT)
  const rest = rows.slice(FACT_ROW_LIMIT)
  for (const r of flat) shownHandles.add(r.handle)
  return { grouped: false, groups: [], hiddenGroups: [], overflowGroups: 0, flat, rest, shownHandles }
}

/**
 * 跨块收集已复算的品类合计，保留块顺序并优先展示 M1。
 * 仅当来源范围、指标、期间与计算事实全等时去重，不合并不同商品子集或品类。
 * 去重只影响页面展示，原始证据与调查记录保持不变。
 */
export function collectGroupTotals(views: BlockView[]): GroupTotalView[] {
  const out: GroupTotalView[] = []
  const seen = new Set<string>()
  for (const v of views) {
    const m1 = v.groupTotals.filter((t) => t.metric === 'M1')
    const rest = v.groupTotals.filter((t) => t.metric !== 'M1')
    for (const t of [...m1, ...rest]) {
      if (seen.has(t.dedupeKey)) continue
      seen.add(t.dedupeKey)
      out.push(t)
    }
  }
  return out
}

/* ---------- 重复 cohort 集合识别（呈现层去重；不改 Wire/raw/证据） ---------- */
/**
 * 权威集合等价签名：同一「范围身份 + 查询条件 + 周期 + 命中/未知成员集」视为等价绑定结果。
 * 只用于**呈现层**判断是否重复铺同一份完整名单与计数；不同集合/条件的块签名不同，各自独立呈现。
 * 签名取自服务端权威段字段（不读模型自由文本）。
 *
 * 范围身份（关键）：完整范围＝命中 ∪ **未命中** ∪ 无法判定，而未命中成员不在权威段名单里
 * （只有非命中计数）。**仅凭 scope_count + 命中/未知名单无法区分「同一批命中、未命中成员不同」
 * 的两个查询**——那会把两个不同业务范围误判为同一集合、隐藏第二份名单。故范围身份优先取
 * 服务端 `scope_signature`（Data 已核实的完整成员集指纹，顺序无关）；旧数据缺该字段时退回
 * **服务端已绑定的集合引用**（同一 ref＝同一次查询的同一权威集合实例，是同一实例的直接身份，
 * 不是从 count/名单推定）；两者都无（信息不足）则用块身份占位，保证两个块永不被判等价——
 * 宁可重复展示，也不误删合法结果。
 */
export function cohortSignature(c: DisplayCohort, blockKey?: string): string {
  const scopeId = c.scope_signature
    ? `sig:${c.scope_signature}`
    : c.ref
      ? `ref:${c.ref}`
      : `block:${blockKey ?? ''}`
  return JSON.stringify([
    scopeId,
    c.scope_kind, c.scope_count,
    c.time_mode, c.period_id, c.base_period_id, c.target_period_id,
    c.conditions.map((k) => [k.metric, k.field, k.operator, k.value]),
    // 成员集按集合比较（顺序无关）：同一成员集不同顺序仍视为同一权威集合
    [...c.match_rows.map((r) => r.product_id)].sort(),
    [...c.unknown_members.map((u) => u.product_id)].sort(),
  ])
}

/**
 * 重复 cohort 块的 key 集（首见者保留完整名单，其后等价块只留一句重复说明；保持块原序）。
 * 例外：`corrected`（含更正横幅）不参与去重——更正说明本身就是该块的独立可见信息，
 * 不能因名单等价被静默收走（ corrected 语义保持）。
 */
export function duplicateCohortKeys(views: BlockView[]): Set<string> {
  const seen = new Set<string>()
  const dup = new Set<string>()
  for (const v of views) {
    const c = v.cohort
    if (!c || c.correctedNote) continue
    if (seen.has(c.signature)) dup.add(v.key)
    else seen.add(c.signature)
  }
  return dup
}

export type BlockView = {
  key: string
  index: number
  type: string
  kindLabel: string
  /** 本块是否有成对（两期）读数：只有成对读数才构成「变化」；块声明为变化型但无成对读数时，
   *  标题不沿用「变化」声明（，与服务端 `has_paired` 判据同一事实形态）。 */
  hasPairedFacts: boolean
  /** comparison/candidate 服务端声明的可比对象/指标/周期概览行（：不再铺完整对象名单） */
  metaText: string | null
  /** 本块声明的对象名单（目录名；收进核查依据折叠区，首屏不铺长商品目录） */
  subjectIds: string[]
  subjectTexts: string[]
  /** comparison 块服务端复算的分组合计（键位恒在；无输入/口径不完整=[]） */
  groupTotals: GroupTotalView[]
  changeRows: FactRowView[]
  factRows: FactRowView[]
  /** 声明范围外的引用读数（已核验事实，只作辅助回查；不上首屏，仍在依据区全量可见） */
  auxRows: FactRowView[]
  medians: MedianGroupView[]
  ranks: RankRowView[]
  /** 工具权威整体排名（集合级；无排名引用/中位数引用=[]，不伪造榜单） */
  toolRankings: ToolRankingView[]
  toolMedians: ToolMedianView[]
  /** 单期集合合计视图（`kind='set_sum'` 引用；键位恒在，无此类引用=[]） */
  singlePeriodTotals: SinglePeriodTotalsView[]
  /** 服务端确定性路径产生的真实引用/口径/计算问题局部说明列表（就近展示） */
  dataIssues: string[]
  /** cohort_list 权威集合视图（其他块型=null；未绑定时不携带集合数据） */
  cohort: CohortView | null
}

/* ---------- 块级映射 ---------- */

const KIND_LABELS: Record<string, string> = {
  metric_change: '指标变化',
  comparison: '对象比较',
  candidate_list: '候选列表',
  cohort_list: '条件筛选集合',
}

/** 块声明为跨期变化、但事实里没有任何成对读数时，本块实际只承载单期读数：标题按事实形态给，
 *  不沿用「变化」这一未被事实支持的身份。块声明本身仍原样留在 wire/调试面，未改写。 */
const SINGLE_PERIOD_READINGS_LABEL = '单期指标读数'
/** 「声明变化」的块类型：它们的两期比较身份必须有成对读数支撑。 */
const CHANGE_CLAIM_TYPES: ReadonlySet<string> = new Set(['metric_change'])

function factRow(f: DisplayFact, scope: TraceScope): FactRowView {
  const unit = f.unit ?? ''
  const withUnit = (v: WireScalar) => withUnitNum(v, unit ? ` ${unit}` : '')
  const hasPair = Boolean(f.base && f.target)
  // 日期窗口事实的时间身份是真实起止日期。窗口事实的 period_id 位＝窗口身份串
  // （`start~end`，服务端回填），periodLabel 目录外原样显示该串；另有结构化 `window` 时优先取它
  // （与 wire 字段同源）。旧周期事实路径逐字不变。
  const windowText = f.window
    ? [f.window.start_date, f.window.end_date].filter(Boolean).join('~')
    : ''
  const periodText = hasPair
    ? `${periodLabel(f.base!.period_id, scope)} → ${periodLabel(f.target!.period_id, scope)}`
    : (windowText || periodLabel(f.period_id, scope) || '—')
  const readingText = hasPair
    ? `${withUnit(f.base!.value)} → ${withUnit(f.target!.value)}`
    : withUnit(f.value)
  const changeBits: string[] = []
  if (f.absolute !== null) changeBits.push(`${withUnit(f.absolute)}`)
  const pct = fmtPct(f.percent)
  if (pct) changeBits.push(`（${pct}）`)
  const dir = f.direction ? DIR_ZH[f.direction] ?? f.direction : ''
  if (dir) changeBits.push(`· ${dir}`)
  const baseNum = f.base ? f.base.value : null
  const targetNum = f.target ? f.target.value : f.value
  const isNum = (v: WireScalar) => typeof v === 'number' && Number.isFinite(v)
  // 逐日读数序列（`day_series` 事实专用）。只做确定性格式化——日期原样、数值千分位、
  // unknown 状态串原样、缺失不代 0；序列为空＝不出该段。
  const series = (f.day_series ?? []).map((d, k) => ({
    key: `${f.handle}-s${k}`,
    dateText: d.date ?? '—',
    valueText: cohortReadingText(d.value),
    stateText: d.state && d.state !== 'ok' ? cohortReadingText(d.state) : '',
  }))
  const seriesText = series
    .map((s) => `${s.dateText.slice(5)} ${s.valueText}${s.stateText ? `（${s.stateText}）` : ''}`)
    .join(' · ')
  return {
    key: f.handle,
    handle: f.handle,
    ref: f.ref,
    productId: f.product_id,
    metric: f.metric,
    periodId: f.period_id,
    timeKey: f.period_id || windowText || null,
    objectText: objText(f.product_id, scope),
    metricText: metricText(f.metric, scope),
    periodText,
    readingText,
    changeText: changeBits.join(''),
    kindText: opLabel(f.kind),
    stateText: f.state ? cohortReadingText(f.state) : '',
    hasPair,
    baseLabel: f.base?.period_id ? periodLabel(f.base.period_id, scope) : '',
    baseText: hasPair ? withUnit(f.base!.value) : '',
    targetLabel: f.target?.period_id ? periodLabel(f.target!.period_id, scope) : '',
    targetText: hasPair ? withUnit(f.target!.value) : '',
    directionText: dir,
    //  首屏行字段：色档只认工具 direction 与状态 token，不从数值重推方向
    baseNumText: cohortReadingText(baseNum),
    targetNumText: cohortReadingText(targetNum),
    baseNumeric: isNum(baseNum),
    targetNumeric: isNum(targetNum),
    unitText: unit,
    pctText: pct,
    tone: (f.state && f.state !== 'ok') || !isNum(f.absolute)
      ? 'unknown'
      : f.direction === 'up' ? 'up' : f.direction === 'down' ? 'down'
        : f.direction === 'flat' ? 'flat' : 'unknown',
    metricNameText: metricName(f.metric, scope),
    windowText,
    series,
    seriesText,
    declaredScope: true,
  }
}

/** 本条事实是否落在块声明范围（metrics/periods）内。
 *
 * 声明＝本块要**直接展示**的数据范围（`CURRENT_FINAL_ANSWER_INSTRUCTION` 展示意图段）：
 * - 指标：块声明了 metrics 时，未声明的指标＝声明外（basis 允许带跨指标上下文引用）；
 * - 周期：diff 可合法跨基期与对比期（即使 periods 只声明对比期）⇒ 两侧任一落在声明内即算
 *   声明内；单期事实的 period_id ∈ 声明集才算声明内；
 * - 声明数组为空（旧/宽松块）＝无法判定 ⇒ 一律算声明内（不猜，保持既有行为）。
 */
function factInDeclaredScope(f: DisplayFact, metrics: string[], periods: string[]): boolean {
  if (metrics.length && f.metric && !metrics.includes(String(f.metric))) return false
  if (periods.length) {
    const ps = new Set<string>()
    if (f.period_id) ps.add(String(f.period_id))
    if (f.base?.period_id) ps.add(String(f.base.period_id))
    if (f.target?.period_id) ps.add(String(f.target.period_id))
    if (ps.size) {
      const values = [...ps]
      // 日期窗口事实的时间身份是真实日期区间（`start~end`），与目录周期 id 不同域——
      // 不与周期声明集做交集判定（否则窗口读数会被误判成「声明外」而掉出首屏）。
      if (values.some((p) => p.includes('~'))) return true
      if (!values.some((p) => periods.includes(p))) return false
    }
  }
  return true
}

/** (对象, 指标, 周期) 归一键：判「成对变化是否已覆盖这条单期读数」，零复算、纯身份比对。 */
function factKey(pid: unknown, metric: unknown, period: unknown): string {
  return `${String(pid ?? '')}\u0000${String(metric ?? '')}\u0000${String(period ?? '')}`
}

/** 候选与指标变化块的 subjects 限定展示对象；comparison 的 subjects 表示焦点，不限制基准成员。 */
const SUBJECT_DISPLAY_TYPES: ReadonlySet<string> = new Set(['candidate_list', 'metric_change'])

export function buildBlockView(b: DisplayBlock, scope: TraceScope): BlockView {
  // 首屏只展示声明范围内的事实；完整事实与声明外引用仍保留在依据区。
  const declaredMetrics = b.display.metrics.map((m) => String(m))
  const declaredPeriods = b.display.periods.map((p) => String(p))
  const declaredSubjects = b.display.subjects.map((s) => String(s))
  // 按块语义限制对象：comparison 的基准成员不套用焦点商品过滤。
  const subjectScoped = SUBJECT_DISPLAY_TYPES.has(b.type) && declaredSubjects.length > 0
  const rows = b.verified_facts.map((f) => ({
    row: factRow(f, scope),
    fact: f,
    inScope: factInDeclaredScope(f, declaredMetrics, declaredPeriods)
      && (!subjectScoped || declaredSubjects.includes(String(f.product_id ?? ''))),
  }))
  const factRows = rows.map((r) => ({ ...r.row, declaredScope: r.inScope }))
  const auxRows = factRows.filter((r) => !r.declaredScope)
  const scoped = rows.filter((r) => r.inScope)
  // 两期读数已覆盖的单期值不重复展示；其它指标的单期读数仍可并列。
  const pairedCovered = new Set<string>()
  for (const r of scoped) {
    const f = r.fact
    if (!(f.base && f.target)) continue
    for (const side of [f.base, f.target]) {
      if (side?.period_id) pairedCovered.add(factKey(f.product_id, f.metric, side.period_id))
    }
  }
  const pairedRows = scoped
    .filter((r) => r.fact.base && r.fact.target)
    .map((r) => ({ ...r.row, declaredScope: true }))
  const hasPairedFacts = pairedRows.length > 0
  // 无成对事实时按实际单期读数呈现，不显示无依据的变化栏。
  // comparison 的单期读数仅展示已声明焦点商品；基准与状态类事实留在依据区。
  // 目录周期与真实日期窗口使用相同展示规则。
  const comparisonFocus = b.type === 'comparison' && declaredSubjects.length > 0
    ? new Set(declaredSubjects)
    : null
  const comparisonSingleKinds: ReadonlySet<string> = new Set(['object', 'window_object'])
  const singleForFirstScreen = (f: DisplayFact): boolean => {
    if (f.base && f.target) return false                                   // 成对变化走 pairedRows
    if (pairedCovered.has(factKey(f.product_id, f.metric, f.period_id))) return false
    if (b.type !== 'comparison') return true
    if (!comparisonFocus) return false
    return comparisonSingleKinds.has(String(f.kind))
      && comparisonFocus.has(String(f.product_id ?? ''))
  }
  const singleRows = scoped
    .filter((r) => singleForFirstScreen(r.fact))
    .map((r) => ({ ...r.row, declaredScope: true }))
  const showSingle = b.type === 'candidate_list' || CHANGE_CLAIM_TYPES.has(b.type)
    || b.type === 'comparison'
  const changeRows = [...pairedRows, ...(showSingle ? singleRows : [])]

  const medians: MedianGroupView[] = b.recomputed.median_by_group.map((g, i) => ({
    key: `${g.metric ?? 'm'}-${g.period_id ?? 'p'}-${i}`,
    title: `${metricText(g.metric, scope)} · ${periodLabel(g.period_id, scope) || '—'}`,
    medianText: cohortReadingText(g.median),
    nText: String(g.n ?? g.members.length),
    basisText: basisLabel(g.basis),
    members: g.members.map((m, j) => ({
      key: `${m.handle ?? 'm'}-${j}`,
      productText: objText(m.product_id, scope),
      valueText: cohortReadingText(m.value),
    })),
    excluded: g.excluded.map((e, j) => ({
      key: `${e.handle ?? 'e'}-${j}`,
      productText: objText(e.product_id, scope),
      stateText: e.state ? cohortReadingText(e.state) : '',
    })),
  }))

  const ranks: RankRowView[] = b.recomputed.ranks.map((r, i) => ({
    key: `${r.handle ?? 'r'}-${i}`,
    productText: objText(r.product_id, scope),
    valueText: cohortReadingText(r.value),
    rankText: r.rank_desc !== null && r.set_n !== null
      ? `降序第 ${r.rank_desc} / 共 ${r.set_n}${r.rank_asc !== null ? `（升序第 ${r.rank_asc}）` : ''}`
      : '—',
    basisText: basisLabel(r.basis),
  }))

  // 工具权威整体排名（集合级）——名次/顺序/原值逐字来自服务端下发的 Data `ranking`，
  // 本层零复算、不重排名、不按渲染序号重生成名次；缺失项由服务端计数如实标注，不挤进榜单尾名。
  // 旧 SSE / 手写夹具缺 `rankings` 键＝空榜单（不伪造、不崩）。
  const toolRankings: ToolRankingView[] = (b.recomputed.rankings ?? []).map((rk, i) => {
    const windowText = rk.window
      ? [rk.window.start_date, rk.window.end_date].filter(Boolean).join('~')
      : ''
    const timeText = windowText || (rk.period_id ? periodLabel(rk.period_id, scope) : '')
    const title = [rk.metric ? metricText(rk.metric, scope) : '', timeText]
      .filter(Boolean).join(' · ') || '本次集合排名'
    const scopeBits: string[] = []
    if (typeof rk.selected_n === 'number') scopeBits.push(`本次选中 ${rk.selected_n} 个商品`)
    if (typeof rk.usable_n === 'number') scopeBits.push(`可用读数 ${rk.usable_n}`)
    if (typeof rk.missing_n === 'number' && rk.missing_n > 0) {
      scopeBits.push(`缺失 ${rk.missing_n}（不参与排名，不计入名次）`)
    }
    return {
      key: `${rk.handle ?? 'rk'}-${i}`,
      title,
      metric: rk.metric,
      periodId: rk.period_id || windowText || null,
      productIds: rk.product_ids,
      unitText: rk.unit ?? '',
      scopeText: scopeBits.join(' · '),
      ref: rk.ref,
      handle: rk.handle,
      missingGroup: rk.missing_group.map((m) => ({
        productId: m.product_id,
        productText: objText(m.product_id, scope),
        valueText: withUnitNum(m.value, rk.unit ? ` ${rk.unit}` : ''),
        stateText: m.state ? cohortReadingText(m.state) : '',
        reasonText: m.reason ?? '',
      })),
      collectionBaselines: [],
      rows: rk.rows.map((row, j) => {
        const val = row.value
        const missing = val === null || val === undefined
          || (typeof val === 'string' && val.startsWith('unknown'))
        return {
          key: `${rk.handle ?? 'rk'}-${j}`,
          productId: row.product_id,
          rankText: typeof row.rank === 'number' ? `第 ${row.rank} 名` : '—',
          rankNum: typeof row.rank === 'number' ? row.rank : null,
          productText: objText(row.product_id, scope),
          valueText: missing ? '—' : withUnitNum(val, rk.unit ? ` ${rk.unit}` : ''),
          isMissing: missing,
          missingText: missing ? (typeof val === 'string' ? cohortReadingText(val) : '本次未提供') : '',
          relatedFacts: [],
        }
      }),
    }
  })

  const toolMedians = b.recomputed.tool_returned.map((t, i) => ({
    key: `${t.metric ?? 'm'}-${t.period_id ?? 'p'}-${i}`,
    label: t.label || '集合中位数',
    metric: t.metric,
    metricText: t.metric ? metricText(t.metric, scope) : '',
    periodId: t.period_id,
    periodText: t.period_id ? periodLabel(t.period_id, scope) : '',
    productIds: t.product_ids,
    unitText: t.unit ?? '',
    medianText: withUnitNum(t.median, t.unit ? ` ${t.unit}` : ''),
    handle: t.handle,
    ref: t.ref,
  }))

  // 概览行（ 长 metadata 减法）：首屏只留帮助判断的**品类/对象规模/指标/周期**；
  // 完整对象名单（12/19 个商品）改由核查依据折叠区承载，默认页不再出现长商品目录。
  // 品类优先取服务端已复算的 `group_totals[].category`（目录可信字段）；无合计时不给品类。
  const subjectTexts = b.display.subjects.map((p) => objText(p, scope))
  // 汇总的「来源范围」＝本块声明对象集（`_recompute_group_totals` 的输入面）；排序后作范围键，
  // 顺序不同但成员集相同＝同一来源范围。
  const subjectScopeKey = b.display.subjects.length
    ? [...b.display.subjects].sort().join('\u0000')
    : ''
  const groupTotals = b.recomputed.group_totals.map((t, i) =>
    buildGroupTotalView(t, i, scope, `block-${b.index}`, subjectScopeKey, declaredSubjects))
  // 单期集合合计视图——只渲染 `verified_facts` 中 `kind='set_sum'` 且携带
  // `single_period_totals` 的实际合计引用；不自动为普通多商品 value 加卡、不从 subjects 拼总数。
  const singlePeriodTotals: SinglePeriodTotalsView[] = []
  for (const f of b.verified_facts) {
    if (String(f.kind ?? '') !== 'set_sum') continue
    const spt = f.single_period_totals
    if (!spt) continue
    const sptKey = spt.sum !== null && typeof spt.sum === 'string' ? spt.sum : null
    const unknownStr = sptKey ? sptKey : null
    const complete = spt.complete === true
    const allMissing = (spt.non_comparable_n ?? 0) > 0 && (spt.comparable_n ?? 0) === 0
    let label: string
    if (allMissing || (unknownStr && unknownStr.startsWith('unknown'))) {
      label = '无可用数据'
    } else if (complete) {
      label = '本次集合完整合计'
    } else {
      label = '已知可用值合计（不含缺失）'
    }
    const selectedN = spt.selected_n
    const comparableN = spt.comparable_n
    const nonComparableN = spt.non_comparable_n ?? 0
    const byState = Object.entries(spt.non_comparable_by_state ?? {})
    const covBits: string[] = []
    if (typeof selectedN === 'number') covBits.push(`请求 ${selectedN} 个成员`)
    if (typeof comparableN === 'number') covBits.push(`可用 ${comparableN}`)
    if (nonComparableN > 0) covBits.push(`缺失/不可用 ${nonComparableN}`)
    const coverageText = covBits.join(' · ')
    const ncBits: string[] = []
    if (byState.length) ncBits.push(byState.map(([s, n]) => `${cohortReadingText(s)} ${n}`).join('、'))
    const nonComparableText = nonComparableN > 0 ? `${nonComparableN} 个不可用${ncBits.length ? `（${ncBits.join('、')}）` : ''}` : ''
    const timeText = (spt.start_date && spt.end_date)
      ? `${spt.start_date} ~ ${spt.end_date}`
      : (spt.period_id ? periodLabel(spt.period_id, scope) : (spt.window_identity ?? '—'))
    const timeIdentity = spt.period_id
      ? `period:${spt.period_id}`
      : (spt.start_date && spt.end_date)
        ? `window:${spt.start_date}~${spt.end_date}`
        : spt.window_identity ? `window:${spt.window_identity}` : null
    const unitTxt = spt.unit ? ` ${spt.unit}` : ''
    const sptNum = withUnitNum(spt.sum, unitTxt)
    singlePeriodTotals.push({
      key: `${f.handle ?? 'spt'}-${spt.metric ?? 'm'}`,
      metric: spt.metric,
      metricText: spt.metric ? metricText(spt.metric, scope) : '—',
      unitText: spt.unit ?? '',
      sumText: sptNum,
      label,
      timeText,
      timeIdentity,
      coverageText,
      nonComparableText,
      productIds: (spt.product_ids ?? []).map((id) => String(id)),
      handle: f.handle ?? null,
      ref: f.ref ?? null,
      //  跨类型覆盖判定用内部字段（wire 原值，零复算；不进可见文案）
      selectedN: spt.selected_n,
      comparableN: spt.comparable_n,
      nonComparableN: spt.non_comparable_n,
      nonComparableByState: spt.non_comparable_by_state ?? {},
      sumScope: spt.sum_scope,
      sumValue: spt.sum,
    })
  }
  const trustedCats: string[] = []
  for (const t of groupTotals) if (t.categoryTrusted && !trustedCats.includes(t.categoryText)) trustedCats.push(t.categoryText)
  const metaBits: string[] = []
  if (trustedCats.length) metaBits.push(`品类：${trustedCats.join('、')}`)
  if (subjectTexts.length) metaBits.push(`对象：${subjectTexts.length} 个商品`)
  if (b.display.metrics.length) {
    metaBits.push(`指标：${b.display.metrics.map((m) => metricText(m, scope)).join('、')}`)
  }
  // 声明为「变化」却无成对读数的块，不铺「周期：A → B」这一未被事实支持的比较声明
  // （各条读数按自己的实际周期单列，逐行口径就近可见）。声明本身仍在 wire/调试面，未被改写。
  const showDeclaredPeriods = !(CHANGE_CLAIM_TYPES.has(b.type) && !hasPairedFacts)
  if (showDeclaredPeriods && b.display.periods.length) {
    metaBits.push(`周期：${b.display.periods.map((p) => periodLabel(p, scope)).join(' → ')}`)
  }
  const metaText = metaBits.length ? metaBits.join('　') : null

  return {
    key: `block-${b.index}`,
    index: b.index,
    type: b.type,
    kindLabel: CHANGE_CLAIM_TYPES.has(b.type) && !hasPairedFacts && factRows.length > 0
      ? SINGLE_PERIOD_READINGS_LABEL
      : (KIND_LABELS[b.type] ?? b.type),
    hasPairedFacts,
    metaText,
    subjectIds: declaredSubjects,
    subjectTexts,
    groupTotals,
    changeRows,
    factRows,
    auxRows,
    medians,
    ranks,
    toolRankings,
    toolMedians,
    singlePeriodTotals,
    dataIssues: b.data_issues,
    cohort: b.cohort ? buildCohortView(b.cohort, scope, `block-${b.index}`) : null,
  }
}

/** 将 display_blocks 映射为块视图；无可用块时返回空数组，由调用方展示正文。 */
export function buildBlockViews(db: DisplayBlocks | null, scope: TraceScope): BlockView[] {
  if (!db || !db.blocks.length) return []
  return db.blocks.map((b) => buildBlockView(b, scope))
}

/**
 *  §4：投影降级的**视图层收口**——`filterDisplayBlocks` 已保证形状合法，此处只判
 * 「有没有可呈现业务结果」：空壳块（纯 `data_issues` 说明、无任何结果数据）不展示；
 * 移除空壳后仍无有效业务块＝整份投影不可用，返回 null，与「缺失/null」同路走一次已有
 * 完整规范正文（结构化卡片形状随投影一并恢复），空壳块不渲染、不补写内容。
 * 合法零命中＋未知 cohort、合法空榜（带范围/时间/缺失明细）都是有效块，不按行数为 0
 * 或缺数值判无效。
 */
export function usableDisplayBlocks(db: DisplayBlocks | null, scope: TraceScope): DisplayBlocks | null {
  if (!db || !db.blocks.length) return null
  const kept = db.blocks.filter((b) => hasPresentableResult(buildBlockView(b, scope)))
  return kept.length ? { ...db, blocks: kept } : null
}

/* ---------- 结果组织层——相邻同类型块合并为一组关键结果 ---------- */

/**
 * 组视图：合并只是**呈现收纳**——同类型的连续块共用一个区块外壳（一份依据区）；元信息与
 * 局部说明按块保留，相邻候选集合的数据按原块分段，其余类型保留既有组级读数单位。依据区
 * 聚合全是数组连接与按键取子视图，不产生任何新数值。
 */
export type BlockGroupView = {
  key: string
  type: string
  kindLabel: string
  /** 原块顺序保留：每块的 meta/数据区/局部说明按块渲染 */
  blocks: BlockView[]
  /** 变化行按组合并以执行跨组 handle 去重；组件仅对相邻候选集合按行 key 中的原 block key
   *  分段呈现，其余类型保留组级读数单位。全部事实仍在依据区读数表内。 */
  changeRows: FactRowView[]
  /** 依据区（默认折叠）内容：组内各块聚合 */
  medians: MedianGroupView[]
  ranks: RankRowView[]
  toolRankings: ToolRankingView[]
  toolMedians: ToolMedianView[]
  singlePeriodTotals: SinglePeriodTotalsView[]
  /** 被完整榜单承载读数的严格子集候选块，其名单关系仍在原候选组呈现。 */
  candidateMemberships: CandidateMembershipView[]
}

export type CandidateMembershipView = {
  blockKey: string
  rankingTitle: string
  members: { key: string; productText: string; handle: string | null }[]
}

const withKey = <T extends { key: string }>(b: BlockView, rows: T[]): T[] =>
  rows.map((r) => ({ ...r, key: `${b.key}:${r.key}` }))

function sameProductSet(left: string[], right: string[]): boolean {
  if (!left.length || left.length !== right.length) return false
  const a = [...left].sort()
  const b = [...right].sort()
  return a.every((id, i) => id === b[i])
}

function rankingCarriesFact(ranking: ToolRankingView, fact: FactRowView): boolean {
  if (fact.hasPair || fact.series.length || !fact.productId || !fact.timeKey) return false
  return ranking.periodId === fact.timeKey
    && ranking.rows.some((row) => row.productId === fact.productId)
}

type CandidateBlockMode = 'compact' | 'independent' | 'normal'

/**
 * 候选块与完整排名存在部分/更大范围重叠时，保留候选块原始读数；只有候选集合严格
 * 包含于同指标/实际时间的完整排名集合时，才允许轻量名单替代重复读数。完全相同集合仍去重。
 */
function candidateBlockModes(groups: BlockGroupView[]): Map<string, CandidateBlockMode> {
  const rankings = groups.flatMap((g) => g.blocks.flatMap((b) => b.toolRankings))
  const modes = new Map<string, CandidateBlockMode>()
  for (const g of groups) {
    for (const block of g.blocks) {
      if (block.type !== 'candidate_list' || !block.subjectIds.length) continue
      const subjects = [...new Set(block.subjectIds)]
      const matching = rankings.filter((ranking) =>
        block.changeRows.some((fact) => rankingCarriesFact(ranking, fact)))
      if (matching.some((ranking) => sameProductSet(subjects, ranking.productIds))) continue
      if (matching.some((ranking) => {
        const productIds = new Set(ranking.productIds)
        return subjects.length < productIds.size && subjects.every((id) => productIds.has(id))
      })) {
        modes.set(block.key, 'compact')
        continue
      }
      if (matching.some((ranking) =>
        subjects.some((id) => ranking.productIds.includes(id)))) {
        modes.set(block.key, 'independent')
      }
    }
  }
  return modes
}

function candidateMembershipViews(
  groups: BlockGroupView[],
  modes: Map<string, CandidateBlockMode>,
): Map<string, CandidateMembershipView> {
  const rankings = groups.flatMap((g) => g.blocks.flatMap((b) => b.toolRankings))
  const memberships = new Map<string, CandidateMembershipView>()
  for (const g of groups) {
    for (const block of g.blocks) {
      if (modes.get(block.key) !== 'compact') continue
      const subjectIds = [...new Set(block.subjectIds)]
      const ranking = rankings
        .filter((candidate) => {
          const ids = new Set(candidate.productIds)
          return subjectIds.length < ids.size
            && subjectIds.every((id) => ids.has(id))
            && block.changeRows.some((fact) => rankingCarriesFact(candidate, fact))
        })
        .sort((a, b) => a.productIds.length - b.productIds.length)[0]
      if (!ranking) continue
      memberships.set(block.key, {
        blockKey: block.key,
        rankingTitle: ranking.title,
        members: subjectIds.map((productId) => {
          const index = block.subjectIds.indexOf(productId)
          const fact = block.factRows.find((candidate) =>
            candidate.productId === productId && rankingCarriesFact(ranking, candidate))
          return {
            key: productId,
            productText: block.subjectTexts[index] ?? productId,
            handle: fact?.handle ?? null,
          }
        }),
      })
    }
  }
  return memberships
}

function keepFirstByEvidenceIdentity<T extends { handle: string | null; ref: string | null }>(
  rows: T[],
  seen: Set<string>,
): T[] {
  return rows.filter((row) => {
    const keys = [
      row.handle ? `handle:${row.handle}` : '',
      row.ref ? `ref:${row.ref}` : '',
    ].filter(Boolean)
    if (keys.some((key) => seen.has(key))) return false
    for (const key of keys) seen.add(key)
    return true
  })
}

/**
 * 按真实商品集合、指标与时间构造单期合计的内容身份。
 * 同一合计可能被不同查询引用，不能仅按 handle/ref 去重；不同范围或口径分别展示。
 */
function singlePeriodTotalsContentKey(v: SinglePeriodTotalsView): string {
  const members = [...(v.productIds ?? [])].sort()
    .join(',')
  const time = v.timeIdentity || `unresolved:${v.ref ?? v.handle ?? v.key}`
  return `${members}|${v.metric ?? ''}|${time}`
}

/**
 * 跨类型内容覆盖判断——两期品类 diff 的分组合计（`comparison` 块
 * `recomputed.group_totals`）是否**完整承载**对应单期集合合计（`set_sum` 引用）。
 *
 * 背景：low 推理强度下同一问题可能先用 `value aggregate=sum` 查各品类单期合计、再用
 * `diff` 查两期合计及变化——结果页同时出现「品类两期对比」与「单期指标读数」，
 * 同一合计（如 118,400 单 / 111,800 单）铺两遍（真机 `ui-fff8233ff5d3`）。
 *
 * 证成条件全部取自已有可信结构化数据（零重算；不凭显示文案、品类名或两个合计数值相等
 * 推断重复）：
 *   1. 指标相同（M1/M2 代号直比，非展示名）；
 *   2. 单期合计的 Data 原时间身份＝分组合计的目标期（`period:<target_period_id>`；
 *      日期窗口/其他期不匹配——窗口与目录周期不同域）；
 *   3. 真实商品成员集合相等（分组合计成员由声明对象集 × 目录品类推导；单期合计取 Data
 *      原 `product_ids`；按集合比对、顺序无关；不同子集即使合计恰好相同也不覆盖）；
 *   4. 参与计算的数据覆盖口径一致：选中/可比/不可比计数与按状态分类全等（分组合计只含
 *      两期均可比成员，口径一致才说明其目标期合计与单期合计同源同覆盖）；
 *   5. 合计数值相等（类型化严格相等）作最后佐证。
 * 任一条件不成立＝无法证实完全覆盖 → 返回 false（保留单期项）。
 */
function groupTotalCoversSetSum(gt: GroupTotalView, s: SinglePeriodTotalsView): boolean {
  if (!gt.metric || gt.metric !== s.metric) return false
  if (!gt.targetPeriodId) return false
  if (s.timeIdentity !== `period:${gt.targetPeriodId}`) return false
  if (!gt.memberIds.length || gt.memberIds.length !== s.productIds.length) return false
  const gtMembers = [...gt.memberIds].sort()
  const sMembers = [...s.productIds].map(String).sort()
  if (gtMembers.some((id, i) => id !== sMembers[i])) return false
  if (gt.subjectsN === null || gt.subjectsN !== s.selectedN) return false
  if (gt.comparableN === null || gt.comparableN !== s.comparableN) return false
  if ((gt.nonComparableN ?? 0) !== (s.nonComparableN ?? 0)) return false
  const byState = (m: Record<string, number> | undefined) =>
    JSON.stringify(Object.entries(m ?? {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
  if (byState(gt.nonComparableByState) !== byState(s.nonComparableByState)) return false
  if (typeof gt.targetSumValue !== 'number' || typeof s.sumValue !== 'number') return false
  return gt.targetSumValue === s.sumValue
}

/**
 *  §2：**跨组**关联权威排名与普通读数——全部组的排名行 × 全部组的单期事实。
 * 结果类型不同不阻止同一结果去重：与某榜单行在指标/实际窗口/对象上同一的事实由该行承载
 * （同指标＝已被代表，移出事实组首屏；不同指标＝并入该行的「同周期已查指标」），
 * 成对读数、逐日序列、其他周期与不含该对象的集合一律不关联（不同来源/集合/窗口/指标
 * 不机械合并；不凭商品名猜 ID，只按真实 `product_id` + 实际时间键匹配）。
 *
 * 返回被**移出**原组、改由榜单行承载的 handle 集合（代表＝已在榜单行给出同值；
 * 并入＝已在该行「同周期已查指标」给出），供冗余组判定收尾。
 */
function associateRankingFacts(
  groups: BlockGroupView[],
  candidateModes: Map<string, CandidateBlockMode>,
): Set<string> {
  const carried = new Set<string>()
  const rankRows = groups.flatMap((g) =>
    g.toolRankings.flatMap((ranking) => ranking.rows.map((row) => ({ ranking, row }))))
  for (const g of groups) {
    const carriedRows = new Set<string>()
    for (const fact of g.changeRows) {
      const source = g.blocks.find((block) => fact.key.startsWith(`${block.key}:`))
      if (source && candidateModes.get(source.key) === 'independent') continue
      if (fact.hasPair || fact.series.length || !fact.productId || !fact.metric || !fact.timeKey) continue
      const matching = rankRows.filter(({ ranking, row }) =>
        row.productId === fact.productId && ranking.periodId === fact.timeKey)
      if (matching.some(({ ranking }) => ranking.metric === fact.metric)) {
        carriedRows.add(fact.key)
        carried.add(fact.handle)
        continue
      }
      const target = matching.find(({ ranking }) => ranking.metric !== fact.metric)
      if (target) {
        target.row.relatedFacts.push(fact)
        carriedRows.add(fact.key)
        carried.add(fact.handle)
      }
    }
    // Exact same-object/metric/time facts are already represented by Data's rank row.
    // Other single-period metrics move into that row once; both keep their original handles
    // in Basis or the inline citation respectively, while paired/other-time facts remain.
    // 同一 handle 可属于不同候选结果；只移走本次已承载的行，保留独立范围中的行。
    g.changeRows = g.changeRows.filter((fact) => !carriedRows.has(fact.key))
  }
  return carried
}

/**  §2：集合中位数并入**同集合同周期**的权威榜单（跨组）——同一阅读单位内给基准，
 *  不再单列「集合基准读数」；成员集合或周期不同不并入（相同数字不同集合不机械合并）。 */
function attachMatchingCollectionMedians(groups: BlockGroupView[]): Set<string> {
  const allRankings = groups.flatMap((g) => g.toolRankings)
  const coveredGroups = new Set<string>()
  for (const g of groups) {
    const remaining: ToolMedianView[] = []
    let attached = 0
    for (const median of g.toolMedians) {
      const ranking = allRankings.find((candidate) =>
        candidate.periodId !== null
        && candidate.periodId === median.periodId
        && sameProductSet(candidate.productIds, median.productIds))
      if (ranking) {
        ranking.collectionBaselines.push(median)
        attached += 1
      }
      else remaining.push(median)
    }
    g.toolMedians = remaining
    if (attached > 0 && remaining.length === 0) coveredGroups.add(g.key)
  }
  return coveredGroups
}

/**
 * 结果分组（呈现收纳）。`views`＝参与分组的块视图；`allViews`（缺省＝`views`）＝跨类型
 * 覆盖判定的完整证据面——调用方（`DisplayBlocks`）会把纯 `group_totals` 兜底块提前滤出
 * 分组（`isTotalsOnlyFallback`），但其分组合计仍是结果页合法承载，故跨类型覆盖必须看
 * 全量视图，不能只看分组输入。
 */
export function buildBlockGroups(views: BlockView[], allViews: BlockView[] = views): BlockGroupView[] {
  const groups: BlockGroupView[] = []
  for (const v of views) {
    const last = groups[groups.length - 1]
    if (last && last.type === v.type) {
      last.blocks.push(v)
    } else {
      groups.push({
        key: `grp-${v.key}`, type: v.type, kindLabel: v.kindLabel,
        blocks: [v], changeRows: [], medians: [], ranks: [], toolRankings: [], toolMedians: [],
        candidateMemberships: [], singlePeriodTotals: [],
      })
    }
  }
  // 组标题＝组内各块标题一致时用该标题（如同为「单期指标读数」），不一致时回到块类型标题——
  // 组按 type 合并，块标题只可能因「声明为变化但无成对读数」而不同，故此处只做确定性收敛。
  for (const g of groups) {
    const labels = new Set(g.blocks.map((b) => b.kindLabel))
    g.kindLabel = labels.size === 1
      ? g.blocks[0].kindLabel
      : (KIND_LABELS[g.type] ?? g.type)
  }
  const candidateModes = candidateBlockModes(groups)
  //  首屏去重：同一 handle 的事实（同 run 唯一证据）只在首个声明它的组里出行；
  // 模型跨块重复引用同一读数时，后组只留依据区。
  const seenFacts = new Set<string>()
  // 跨块重复声明且零独有内容的组不再单独成卡：全部事实 handle 已在先前组展示、无局部说明/
  // 独立计算面——缺失即被丢弃。
  const seenAll = new Set<string>()
  for (const g of groups) {
    for (const b of g.blocks) {
      const localHandles = new Set<string>()
      const fresh = b.changeRows.filter((r) => {
        if (localHandles.has(r.handle)) return false
        localHandles.add(r.handle)
        if (candidateModes.get(b.key) === 'independent') {
          // 部分重叠/更大候选集保留本块读数，避免只剩排名范围内的成员。
          seenFacts.add(r.handle)
          return true
        }
        if (seenFacts.has(r.handle)) return false
        seenFacts.add(r.handle)
        return true
      })
      g.changeRows.push(...withKey(b, fresh))
      g.medians.push(...withKey(b, b.medians))
      g.ranks.push(...withKey(b, b.ranks))
      g.toolRankings.push(...withKey(b, b.toolRankings).map((ranking) => ({
        ...ranking,
        collectionBaselines: [],
        rows: ranking.rows.map((row) => ({ ...row, relatedFacts: [] })),
      })))
      g.toolMedians.push(...withKey(b, b.toolMedians))
      g.singlePeriodTotals.push(...withKey(b, b.singlePeriodTotals))
    }
  }
  const candidateMemberships = candidateMembershipViews(groups, candidateModes)
  for (const g of groups) {
    g.candidateMemberships = g.blocks.flatMap((b) => {
      const membership = candidateMemberships.get(b.key)
      return membership ? [membership] : []
    })
  }
  // 同一已核验排名/集合中位数引用跨块只保留首次业务承载；handle 与 canonical ref 均可识别身份。
  const seenRankingRefs = new Set<string>()
  const seenMedianRefs = new Set<string>()
  const seenSetSumRefs = new Set<string>()
  const seenSetSumContent = new Set<string>()
  const coveredCollectionGroups = new Set<string>()
  for (const g of groups) {
    const rankingCount = g.toolRankings.length
    const medianCount = g.toolMedians.length
    const setSumCount = g.singlePeriodTotals.length
    g.toolRankings = keepFirstByEvidenceIdentity(g.toolRankings, seenRankingRefs)
    g.toolMedians = keepFirstByEvidenceIdentity(g.toolMedians, seenMedianRefs)
    // 依次按证据身份和内容身份去重，避免补查产生的等价合计重复展示。
    g.singlePeriodTotals = g.singlePeriodTotals.filter((s) => {
      const evidenceKeys = [
        s.handle ? `handle:${s.handle}` : '',
        s.ref ? `ref:${s.ref}` : '',
      ].filter(Boolean)
      if (evidenceKeys.some((key) => seenSetSumRefs.has(key))) return false
      for (const key of evidenceKeys) seenSetSumRefs.add(key)
      const contentKey = singlePeriodTotalsContentKey(s)
      if (contentKey && seenSetSumContent.has(contentKey)) return false
      if (contentKey) seenSetSumContent.add(contentKey)
      return true
    })
    if (g.toolRankings.length < rankingCount
      || g.toolMedians.length < medianCount
      || g.singlePeriodTotals.length < setSumCount) {
      coveredCollectionGroups.add(g.key)
    }
  }
  // 跨类型覆盖——两期品类 diff 的分组合计已完整承载（成员 × 指标 × 目标期 × 数据
  // 覆盖口径全等，见 `groupTotalCoversSetSum`）的单期集合合计不再进首屏重复展示。只改
  // 展示投影：事实/引用/Data 合计/依据回查一律不动；无法证实完全覆盖时保留单期项。
  // 跨组收集全部分组合计（完整承载可能落在另一结果类型的组、或被滤出分组的纯合计兜底
  // 块，类型不同/不进分组不阻止去重——故取全量视图而非仅分组输入）。
  const crossTypeTotals = allViews.flatMap((v) => v.groupTotals)
  for (const g of groups) {
    if (!g.singlePeriodTotals.length || !crossTypeTotals.length) continue
    const before = g.singlePeriodTotals.length
    g.singlePeriodTotals = g.singlePeriodTotals.filter((s) =>
      !crossTypeTotals.some((gt) => groupTotalCoversSetSum(gt, s)))
    if (g.singlePeriodTotals.length < before) coveredCollectionGroups.add(g.key)
  }
  //  §2：先做**跨组**关联（排名 × 全部组的读数、中位数并入同集合同周期榜单），再判冗余——
  // 完整承载可能落在另一结果类型的组，类型不同不阻止去重。
  for (const groupKey of attachMatchingCollectionMedians(groups)) coveredCollectionGroups.add(groupKey)
  const carriedByRanking = associateRankingFacts(groups, candidateModes)
  // 并入同集合同周期榜单的集合中位数同样由榜单阅读区承载（基线句＋引用入口在榜单模块），
  // 其 handle 不算原组独有内容。
  for (const g of groups) {
    for (const ranking of g.toolRankings) {
      for (const baseline of ranking.collectionBaselines) {
        if (baseline.handle) carriedByRanking.add(baseline.handle)
      }
    }
  }
  const kept: BlockGroupView[] = []
  for (const g of groups) {
    // 组级内容判据：跨组移走（读数被排名行承载、中位数并入榜单）后组内无可呈现内容时按
    // 冗余组收尾——同一结果只由一处完整承载，不留空壳组。
    const redundant = g.changeRows.length === 0
      && g.toolRankings.length === 0
      && g.toolMedians.length === 0
      && g.medians.length === 0
      && g.ranks.length === 0
      && g.singlePeriodTotals.length === 0
      && g.candidateMemberships.length === 0
      && g.blocks.every((b) => b.dataIssues.length === 0
        && b.factRows.every((r) => seenAll.has(r.handle) || carriedByRanking.has(r.handle))
        && (b.changeRows.length > 0 || b.toolRankings.length > 0 || b.toolMedians.length > 0))
      || (g.changeRows.length === 0
        && g.toolRankings.length === 0
        && g.toolMedians.length === 0
        && g.medians.length === 0
        && g.ranks.length === 0
        && g.singlePeriodTotals.length === 0
        && g.candidateMemberships.length === 0
        && coveredCollectionGroups.has(g.key)
        && g.blocks.every((b) => b.dataIssues.length === 0
          && b.factRows.every((r) => seenAll.has(r.handle) || carriedByRanking.has(r.handle))))
    for (const b of g.blocks) for (const r of b.factRows) seenAll.add(r.handle)
    if (!redundant) kept.push(g)
  }
  return kept
}
