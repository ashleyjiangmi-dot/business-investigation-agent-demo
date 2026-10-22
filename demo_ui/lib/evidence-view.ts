/**
 * 将已登记证据投影为经营读数详情，只做格式化与排版，不重算或补齐缺失值。
 * M1/M2 按整数、M3 按百分数及百分点变化、M4/M5 按小数展示；原始值保持不变。
 * 主文案使用业务名称，引用和字段标识保留在技术追溯区。
 * 仅展示影响读数解释的范围、缺失和可比性限制。
 */
import type { EvidenceDetail, EvidencePeriod } from './trace-filter'
import { periodLabel, productName, type TraceScope } from './trace'

/** 数据状态 token → 业务语言（服务端 structured unknown 状态码的确定性映射，单点在此）。
 *  缺值类状态写清「是什么缺了、本次能不能判定」，不把缺值读成 0，也不写实现词表口吻。 */
const STATE_ZH: Record<string, string> = {
  ok: '正常读数',
  period_missing: '该周期没有记录，本次无法判定（不等于 0）',
  field_missing: '该指标字段缺失，本次无法判定（不等于 0）',
  field_all_missing: '指标数据缺失，本次无法判定（不等于 0）',
  not_applicable: '该指标按口径不适用于此对象',
  window_incomparable: '两期窗口不可比（周期长度/口径不一致），未给差值',
  zero_denominator: '分母为零或无配对行，未给比率（不等于 0）',
  record_conflict: '同一对象同一周期存在互相矛盾的记录',
  identity_inconsistent: '对象身份跨期不一致，已停止比较',
  state_change: '记录状态跨期发生变化（正常变化）',
  attribute_drift: '品类、城市等属性跨期变化，需复核',
  no_usable_values: '集合内没有可用值，未产出中位数与分组',
  no_available_members: '本次没有可用读数，未返回合计',
  no_comparable_products: '没有两期均可比的商品，未返回合计与变化',
}

/** 引用键形（op）→ 取数方式业务说法（与服务端 op 目录同源；未收录留空不硬译）。 */
const OP_ZH: Record<string, string> = {
  object: '单期取值查询',
  period: '周期引用',
  state: '记录状态核对',
  diff: '两期对比查询',
  compare: '集合比较查询（中位数/排名基准）',
  compare_row: '集合比较结果行',
  compare_missing: '集合比较缺失行',
  object_state: '身份与跨期状态核对',
  catalog: '商品与周期目录读取',
  cohort: '组合条件筛选',
  category_total: '品类汇总查询（两期合计）',
  evidence: '单期取证核对',
  // 单期集合合计引用（value aggregate='sum'）；引用键形 `set_sum:<metric>`，与
  // 品类汇总（category_total，两期）区分。
  set_sum: '单期集合合计',
  //  日期窗口引用（kind 取自 canonical 引用键形；旧周期引用词表不变）
  window: '日期窗口引用',
  window_object: '日期窗口单期取值查询',
  window_diff: '日期窗口两期对比查询',
  window_compare: '日期窗口集合比较查询（中位数/排名基准）',
  window_compare_row: '日期窗口集合比较结果行',
  window_compare_missing: '日期窗口集合比较缺失行',
  day_series: '连续日读数查询',
}

/** 状态字段 token → 条件读数位的业务说法（cohort 条件 field 单点词表）。 */
export const COHORT_FIELD_ZH: Record<string, string> = {
  value: '读数',
  absolute_diff: '两期变化值',
  percent_diff: '两期变化幅度',
}

/**
 * 「变化字段对 0 比较」的业务化词表：指标代号 → 变化动词主语（M1 订单量／M2 播放量）。
 * 只覆盖可加总指标；其他指标不做业务化（退回通用表达，不猜名字）。
 */
const CHANGE_SUBJECT_ZH: Record<string, string> = { M1: '订单', M2: '播放' }

const CHANGE_OP_ZH: Record<string, string> = {
  '>': '上涨',
  '<': '下降',
  '==': '持平',
  '>=': '不下降',
  '<=': '不上涨',
}

/** 集合中位数比较的已确认分组 → 运营可读结果；基准只限本次参与比较的商品集合。 */
const COMPARE_GROUP_ZH: Record<'high' | 'equal' | 'low', string> = {
  high: '高于本次商品集合的中位数',
  equal: '与本次商品集合的中位数持平',
  low: '低于本次商品集合的中位数',
}

/** 阈值是否恰为 0（服务端下发可能是数值 0 或字符串 '0'；两者都算）。 */
function isZeroThreshold(value: string | number | null | undefined): boolean {
  if (value === null || value === undefined) return false
  return value === 0 || String(value).trim() === '0'
}

/** 指标显示名（运营面不带 M1–M5 代号；目录缺失时保留代号，不猜名字）。 */
function metricNameText(metric: string | null, metricName: string | null, scope: TraceScope): string {
  if (metricName) return metricName
  if (!metric) return ''
  return scope.metrics?.[metric] ?? metric
}

/** cohort 条件阈值 → 指标口径文本（M3 比率/绝对差换百分数/百分点，百分比字段补 %）。 */
function cohortThresholdText(metric: string | null, field: string, value: string | number | null): string {
  if (value === null || value === undefined) return '—'
  if (typeof value !== 'number') return String(value)
  if (field === 'percent_diff') return percentText(value)
  if (metric === 'M3') {
    if (field === 'value') return `${formatNumber(value * 100, 2)}%`
    if (field === 'absolute_diff') return `${formatNumber(value * 100, 2)} 个百分点`
  }
  if (metric === 'M4' || metric === 'M5') return formatNumber(value, 2)
  if (metric === 'M1' || metric === 'M2') return formatNumber(value, Number.isInteger(value) ? 0 : 2)
  return formatNumber(value, 4, false)
}

/**
 * cohort 筛选条件 → 人读条件句。
 * 常见「变化字段（absolute_diff / percent_diff）与 0 比较」直接业务化（播放上涨 / 订单下降 /
 * 持平 / 不下降 / 不上涨）——这些条件读起来是经营判断，不是「字段 比较符 数值」。
 * 只有阈值不是 0、字段不是变化字段、指标无可加总业务主语，或比较符无法业务化时，
 * 才保留通用表达（指标名＋字段＋比较符＋阈值）。
 *  单点：display-blocks（集合块条件行）与本文件（证据详情）共用，不建第二套词表。
 */
export function cohortConditionText(
  c: { metric: string | null; field: string | null; operator: string | null; value: string | number | null },
  scope: TraceScope,
): string {
  const field = c.field ?? ''
  const subject = c.metric ? CHANGE_SUBJECT_ZH[c.metric] : undefined
  if (subject && (field === 'absolute_diff' || field === 'percent_diff') && isZeroThreshold(c.value)) {
    const word = CHANGE_OP_ZH[c.operator ?? '']
    if (word) return `${subject}${word}`
  }
  const metric = c.metric ? metricNameText(c.metric, null, scope) : '—'
  const fieldZh = (COHORT_FIELD_ZH[field] ?? field) || '取值'
  const op = { '<': '低于', '<=': '不超过', '>': '高于', '>=': '不低于', '==': '等于', '!=': '不等于' }[c.operator ?? ''] ?? c.operator ?? ''
  const value = cohortThresholdText(c.metric, field, c.value)
  return `${metric} ${fieldZh} ${op} ${value}`
}

/** cohort 条件读数值 → 单元格文本（数值千分位；unknown(...) 转业务状态，不暴露状态码）。 */
export function cohortReadingText(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') return v.toLocaleString('zh-CN')
  const unknown = /^unknown\(([^)]+)\)$/.exec(v.trim())
  if (unknown) return STATE_ZH[unknown[1]] ?? '本次无法判定'
  return /^[a-z][a-z0-9_]*$/.test(v.trim()) ? STATE_ZH[v.trim()] ?? '本次无法判定' : v
}

/** states 行 field token → 业务属性名（确定性映射，不猜）。 */
const FIELD_ZH: Record<string, string> = {
  published: '发布状态',
  price: '价格',
  category: '品类',
  city: '城市',
}

export type DetailLine = { label: string; value: string }

export type DetailView = {
  title: string
  /** 页面主文案行（全部业务语言）；技术追溯信息（handle/ref/代号）另走 rawRef */
  lines: DetailLine[]
  /** 会影响读数解释的范围、缺失或可比性限制；普通读数为空 */
  boundary: string
  rawRef: string
  handle: string
  /** 技术追溯折叠行中的短引用与完整引用映射。 */
  techTrace: string
  /** locate=missing：页面显示「暂不可核对」，不显示任何数值 */
  unavailable: boolean
}

export function stateLabel(state: string | null | undefined): string {
  if (!state) return ''
  return STATE_ZH[state] ?? state   // 未收录 token 原样给（不硬译、不吞状态）
}

/** 引用键形（op/fact.kind）→ 取数方式业务说法（OP_ZH 单点消费；未收录原样给，不硬译）。 */
export function opLabel(op: string | null | undefined): string {
  if (!op) return '本次登记查询'
  return OP_ZH[op] ?? op
}

function unknownStatus(value: string | number | null | undefined): string | null {
  if (typeof value !== 'string') return null
  const match = /^unknown\(([^)]+)\)$/.exec(value)
  return match && Object.prototype.hasOwnProperty.call(STATE_ZH, match[1]) ? match[1] : null
}

function structuredStatus(value: string | number | null | undefined, stateHint?: string | null): string | null {
  const isUnknown = typeof value === 'string' && /^unknown\([^)]+\)$/.test(value)
  return unknownStatus(value) ?? (isUnknown && knownState(stateHint) ? stateHint : null)
}

function knownState(state: string | null | undefined): state is string {
  return !!state && Object.prototype.hasOwnProperty.call(STATE_ZH, state)
}

/**
 * M1–M5 的展示口径（只格式化，不改原始值）：
 * - M1/M2 是可加总的整数读数；
 * - M3 是 0–1 的条均比率，运营面按百分数看；两期绝对变化用百分点；
 * - M4/M5 是每千次播放的效率指标，保留两位小数。
 * 未收录指标不猜业务单位，按原始数值最多 4 位小数、去尾零展示。
 */
type MetricDisplay = {
  digits: number
  scale: number
  unit: string | null
  changeUnit: string | null
  fixed: boolean
}

function metricDisplay(metric: string | null, unit: string | null): MetricDisplay {
  switch (metric) {
    case 'M1':
    case 'M2':
      return { digits: 0, scale: 1, unit, changeUnit: unit, fixed: true }
    case 'M3':
      return { digits: 2, scale: 100, unit: '%（条均）', changeUnit: '个百分点', fixed: true }
    case 'M4':
    case 'M5':
      return { digits: 2, scale: 1, unit, changeUnit: unit, fixed: true }
    default:
      return { digits: 4, scale: 1, unit, changeUnit: unit, fixed: false }
  }
}

const MAX_FRACTION_DIGITS = 8

function numericText(value: number, digits: number, fixed: boolean): string {
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: fixed ? digits : 0,
    maximumFractionDigits: digits,
  })
}

/**
 * 固定业务精度的数字文本；真实非零值若在该精度下舍入成 0，逐位提高精度，
 * 不输出 `-0` / `0.00` 这类会把「有变化」读成「没变化」的假零。
 */
function formatNumber(value: number, digits: number, fixed = true): string {
  if (!Number.isFinite(value)) return String(value)
  if (value === 0) return numericText(0, digits, fixed)
  let d = digits
  let text = numericText(value, d, fixed)
  while (Number(text.replace(/,/g, '')) === 0 && d < MAX_FRACTION_DIGITS) {
    d += 1
    text = numericText(value, d, fixed)
  }
  return text
}

function valueWithUnit(text: string, unit: string | null): string {
  if (!unit) return text
  return unit.startsWith('%') ? `${text}${unit}` : `${text} ${unit}`
}

/** 已是百分数单位的数值（百分比变化、占比）→ 两位小数百分比。 */
function percentText(value: number): string {
  return `${formatNumber(value, 2)}%`
}

function digitsFor(value: number, spec: MetricDisplay): number {
  return spec.digits === 0 && !Number.isInteger(value) ? 2 : spec.digits
}

/** unknown(...) 始终转业务状态；未收录状态不把机器 token 泄露到用户面。 */
function scalarText(
  value: string | number | null | undefined,
  stateHint?: string | null,
  explainedState?: string | null,
): string {
  if (value === null || value === undefined) return '—'
  const status = structuredStatus(value, stateHint)
  if (status) return status === explainedState ? '本次无法判定' : stateLabel(status)
  if (typeof value === 'string' && /^unknown\([^)]+\)$/.test(value.trim())) return '本次无法判定'
  return typeof value === 'number'
    ? formatNumber(value, Number.isInteger(value) ? 0 : 4, false)
    : String(value)
}

/** 读数文本：按指标口径格式化数值并带单位；已知结构化状态用中文状态词表呈现。 */
function readout(
  value: string | number | null,
  unit: string | null,
  metric: string | null,
  stateHint?: string | null,
  explainedState?: string | null,
): string {
  const status = structuredStatus(value, stateHint)
  if (status) return status === explainedState ? '本次无法判定' : stateLabel(status)
  if (typeof value !== 'number') return valueWithUnit(scalarText(value, stateHint, explainedState), unit)
  const spec = metricDisplay(metric, unit)
  return valueWithUnit(formatNumber(value * spec.scale, digitsFor(value, spec), spec.fixed), spec.unit)
}

/** 变化值文本：M3 的绝对变化换算成百分点，其余指标沿用读数口径。 */
function changeReadout(
  value: string | number | null,
  unit: string | null,
  metric: string | null,
  stateHint?: string | null,
  explainedState?: string | null,
): string {
  const status = structuredStatus(value, stateHint)
  if (status) return status === explainedState ? '本次无法判定' : stateLabel(status)
  if (typeof value !== 'number') return valueWithUnit(scalarText(value, stateHint, explainedState), unit)
  const spec = metricDisplay(metric, unit)
  return valueWithUnit(formatNumber(value * spec.scale, digitsFor(value, spec), spec.fixed), spec.changeUnit)
}

type PairSide = { value: string | number | null; unit: string | null; state: string | null }

/** 两期读数共用精度：默认精度下若两侧相同、而原始值不同，则逐位提高两侧精度，避免「读数没变、差值非零」。 */
function pairedReadout(
  base: PairSide,
  target: PairSide,
  metric: string | null,
  fallbackUnit: string | null,
  explainedState?: string | null,
): [string, string] {
  const baseStatus = structuredStatus(base.value, base.state)
  const targetStatus = structuredStatus(target.value, target.state)
  if (baseStatus || targetStatus || typeof base.value !== 'number' || typeof target.value !== 'number') {
    return [
      readout(base.value, base.unit ?? fallbackUnit, metric, base.state, explainedState),
      readout(target.value, target.unit ?? fallbackUnit, metric, target.state, explainedState),
    ]
  }
  const spec = metricDisplay(metric, fallbackUnit)
  const baseUnit = base.unit ?? fallbackUnit
  const targetUnit = target.unit ?? fallbackUnit
  const startDigits = spec.digits === 0 && (!Number.isInteger(base.value) || !Number.isInteger(target.value))
    ? 2 : spec.digits
  const maxDigits = Math.min(MAX_FRACTION_DIGITS, startDigits + 4)
  const pairValue = (value: number, unit: string | null, digits: number) =>
    valueWithUnit(formatNumber(value * spec.scale, digits, spec.fixed), spec.scale === 100 ? spec.unit : unit)
  let digits = startDigits
  let baseText = pairValue(base.value, baseUnit, digits)
  let targetText = pairValue(target.value, targetUnit, digits)
  while (base.value !== target.value && baseText === targetText && digits < maxDigits) {
    digits += 1
    baseText = pairValue(base.value, baseUnit, digits)
    targetText = pairValue(target.value, targetUnit, digits)
  }
  return [baseText, targetText]
}

/** 无单位数字的千分位形态（与依据读数使用同一已知状态映射）。 */
function fmtNum(v: string | number | null | undefined, stateHint?: string | null, explainedState?: string | null): string {
  return scalarText(v, stateHint, explainedState)
}

function reasonLabel(state: string | null | undefined, reason: string | null | undefined): string {
  if (!reason || reason === state) return ''
  if (knownState(reason)) return stateLabel(reason)
  return /^[a-z][a-z0-9_]*$/.test(reason) ? '' : reason
}

function traceStatusFields(d: EvidenceDetail): string {
  if (d.locate === 'missing') return ''
  const fields: string[] = []
  const addState = (label: string, state: string | null | undefined) => {
    if (state && state !== 'ok') fields.push(`${label}=${state}`)
  }
  const addReason = (label: string, reason: string | null | undefined) => {
    if (reason) fields.push(`${label}=${reason}`)
  }
  const addUnknown = (label: string, value: string | number | null | undefined, stateHint?: string | null) => {
    if (typeof value === 'string' && /^unknown\([^)]+\)$/.test(value)
        && (unknownStatus(value) || knownState(stateHint))) fields.push(`${label}=${value}`)
  }
  addState('state', d.state)
  addReason('reason', d.reason)
  addUnknown('value', d.value, d.state)
  for (const [label, side] of [['base', d.base], ['target', d.target]] as const) {
    if (side) {
      addState(`${label}.state`, side.state)
      addUnknown(`${label}.value`, side.value, side.state ?? d.state)
    }
  }
  addUnknown('change.absolute', d.change?.absolute, d.state)
  addUnknown('change.percent', d.change?.percent, d.state)
  d.series?.forEach((point) => {
    const label = point.date ?? 'day'
    addState(`${label}.state`, point.state)
    addReason(`${label}.reason`, point.reason)
    addUnknown(`${label}.value`, point.value, point.state ?? d.state)
  })
  d.metric_rows?.forEach((row) => addUnknown(`${row.metric}.value`, row.value, d.state))
  const collection = d.collection
  if (collection?.missing_group) {
    collection.missing_group.forEach((member) => {
      const label = member.product_id ?? 'member'
      addState(`${label}.state`, member.state)
      addReason(`${label}.reason`, member.reason)
      addUnknown(`${label}.value`, member.value, member.state)
    })
  }
  addUnknown('collection.sum', collection?.sum)
  const percentChange = collection?.percent_change
  if (percentChange === null || typeof percentChange === 'string' || typeof percentChange === 'number') {
    addUnknown('collection.percent_change', percentChange)
  }
  for (const key of ['base_sum', 'target_sum', 'absolute_change', 'median'] as const) {
    const value = collection?.[key]
    if (value === null || typeof value === 'string' || typeof value === 'number') addUnknown(`collection.${key}`, value)
  }
  if (Array.isArray(collection?.ranking)) {
    collection.ranking.forEach((row, index) => addUnknown(`collection.ranking[${index}].value`, row.value))
  }
  if (fields.length === 0) return ''
  return `；原始状态、原因与未判定读数：${fields.join('；')}`
}

/**
 * 依据详情里的周期文本（：**日期优先**）。
 * 服务端块自带真实日期区间时直接给 `2026-05-14~2026-05-20`（真实登记值）；
 * 不再铺「（模拟）」「等长 7 天」这类无必要限定。日期缺失时回退运行时周期目录标签
 * （去掉括注修饰）再退 ID——周期身份不丢，原始字段仍逐字在技术追溯里。
 */
function periodText(p: EvidencePeriod | null, scope: TraceScope): string {
  if (!p) return '本次未提供'
  if (p.start_date && p.end_date) return `${p.start_date}~${p.end_date}`
  const name = p.label ? p.label.replace(/（[^）]*）/g, '').trim() : ''
  return name || periodLabel(p.period_id, scope).replace(/（[^）]*）/g, '') || p.period_id
}

/** 商品身份：名称可明确识别时只给名称；同名歧义或目录查不到时才补 ID（不猜名字）。 */
function productDisplayName(id: string | null, scope: TraceScope): string {
  if (!id) return '商品身份本次未提供'
  const name = productName(id, scope)
  if (name === id) return id
  const sameName = scope.products.filter((p) => (p.scope_label ?? p.product_name) === name).length
  return sameName > 1 ? `${name}（${id}）` : name
}

function metricText(d: EvidenceDetail, scope: TraceScope): string {
  return metricNameText(d.metric, d.metric_name, scope)
}

function stateCountsText(counts: Record<string, unknown>): string {
  return Object.entries(counts)
    .map(([state, n]) => `${stateLabel(state)} ${String(n)}`)
    .join('；')
}

/** 日期窗口响应的聚合粒度（服务端 token 只做确定性业务翻译，未收录不硬译）。 */
function granularityText(note: string): string {
  const raw = /粒度\s+([^)）]+)[)）]?$/.exec(note ?? '')?.[1]
  if (raw === 'day' || raw === 'daily') return '按日'
  if (raw === 'period') return '按周期'
  return ''
}

const DENOMINATOR_KIND_ZH: Record<string, string> = {
  sum_vv: '有效配对播放量（Σvv）',
  row_count: '参与统计的内容条数（条均分母）',
}

/** 比率指标的分母口径；求和类指标（无分母）不再显示技术性的「无分母」说明。 */
function denominatorText(den: EvidenceDetail['denominator']): string {
  if (!den || den.kind === 'sum_no_denominator') return ''
  const label = DENOMINATOR_KIND_ZH[den.kind ?? ''] ?? den.note ?? '分母'
  const valueText = den.value !== null && den.value !== undefined ? fmtNum(den.value) : ''
  return valueText ? `${label}：${valueText}` : label
}

/** 集合成员的不可用读数：数值、状态、原因各说一次，不把同一状态重复铺两行。 */
function memberReadingText(
  value: string | number | null,
  unit: string | null,
  metric: string | null,
  state: string | null,
  reason: string | null,
): string {
  const status = state ?? unknownStatus(value)
  const pieces: string[] = []
  if (typeof value === 'number') pieces.push(readout(value, unit, metric))
  if (status) pieces.push(stateLabel(status))
  const reasonText = reasonLabel(status, reason)
  if (reasonText) pieces.push(reasonText)
  return pieces.length ? pieces.join(' · ') : '本次未提供'
}

function objectStateLabel(state: string): string {
  return state === 'ok' ? '正常' : stateLabel(state)
}

function attributeText(field: string, value: string | number | null): string {
  if (value === null || value === undefined) return '—'
  const raw: unknown = value
  if (field === 'published' && (typeof raw === 'boolean' || raw === 'true' || raw === 'false')) {
    return raw === true || raw === 'true' ? '已发布' : '未发布'
  }
  return String(value)
}

/** 商品身份核对的两期属性变化：同字段合成「基准 → 对比」，不重复两行字段名。 */
function crossPeriodChanges(d: EvidenceDetail): string {
  const base = new Map((d.base?.changes ?? []).map((ch) => [String(ch.field ?? ''), ch.value]))
  const target = new Map((d.target?.changes ?? []).map((ch) => [String(ch.field ?? ''), ch.value]))
  const fields = [...new Set([...base.keys(), ...target.keys()])].filter(Boolean)
  return fields.map((field) => {
    const label = FIELD_ZH[field] ?? field
    if (base.has(field) && target.has(field)) {
      return `${label}：${attributeText(field, base.get(field) ?? null)} → ${attributeText(field, target.get(field) ?? null)}`
    }
    if (base.has(field)) return `${label}（基准周期）：${attributeText(field, base.get(field) ?? null)}`
    return `${label}（对比周期）：${attributeText(field, target.get(field) ?? null)}`
  }).join('；')
}

/** 技术追溯折叠行：短编号 → 完整引用（含服务端登记查询编号，业务面不展示这些内部标识）。 */
function techTraceText(d: EvidenceDetail): string {
  const head = d.handle ? `正文引用 [${d.handle}] → ${d.ref}` : `完整引用 ${d.ref}`
  const trace = d.query_id ? `${head}（查询编号 ${d.query_id}）` : head
  return `${trace}${traceStatusFields(d)}`
}

/** 只返回确实影响读数解释的限制；普通查询结果由详情行本身说明。 */
function boundaryText(d: EvidenceDetail): string {
  if (d.locate === 'missing') return ''
  switch (d.op) {
    case 'diff':
    case 'object':
    case 'window_object':
      return ''
    case 'window_diff': {
      const baseDays = d.base?.length_days
      const targetDays = d.target?.length_days
      return typeof baseDays === 'number' && typeof targetDays === 'number' && baseDays !== targetDays
        ? `两侧日期窗口长度不同（${baseDays} 天 / ${targetDays} 天），变化不代表同进度比较。`
        : ''
    }
    case 'compare':
    case 'window_compare': {
      // 集合范围已在「集合范围 / 缺失成员」行给出；只有确有缺失、影响基准解释时才补一句。
      const c = d.collection ?? {}
      const missing = typeof c.missing_n === 'number' ? c.missing_n : 0
      return missing > 0
        ? `本次有 ${missing} 个商品缺少可用读数，未参与基准计算。`
        : ''
    }
    case 'compare_row':
    case 'window_compare_row':
      return ''
    case 'compare_missing':
    case 'window_compare_missing':
      return '该商品未参与本次基准计算。'
    case 'set_sum':
      // 缺失成员的处理方式就近写在「覆盖」行，不再单独铺一句重复限制。
      return ''
    case 'day_series':
      return ''
    case 'window':
      return '该依据只说明所查日期范围，不含商品读数。'
    case 'catalog':
      return '目录只含商品与周期信息，不含经营读数。'
    case 'cohort': {
      const incomplete = d.collection?.members_complete === false
      return incomplete
        ? '命中名单未完整返回，计数以结果口径为准；名单按目录顺序排列，不代表表现排名。'
        : '名单按目录顺序排列，不代表表现排名。'
    }
    case 'category_total': {
      const c = d.collection ?? {}
      const selected = typeof c.selected_n === 'number' ? c.selected_n : null
      const comparable = typeof c.comparable_n === 'number' ? c.comparable_n : null
      const nonComparable = typeof c.non_comparable_n === 'number' ? c.non_comparable_n : null
      const catalog = typeof c.catalog_member_count === 'number' ? c.catalog_member_count : null
      const isValueShape = c.base_sum == null && c.target_sum == null
        && (c.aggregate_kind === 'category_total' || c.aggregate_kind === 'category_selected_total')
      const selectedScope = c.member_scope === 'selected_members_only'
      const fullCategory = c.member_scope === 'full_category'
        || (isValueShape && !selectedScope && catalog !== null
            && selected !== null && selected === catalog)
      if (nonComparable !== null && nonComparable > 0) {
        if (selectedScope) {
          const scope = selected !== null
            ? `合计只代表本次选中的 ${selected} 个商品`
            : '合计只代表本次选中的商品'
          const coverage: string[] = []
          if (comparable !== null) coverage.push(`可比 ${comparable} 个`)
          coverage.push(`不可比 ${nonComparable} 个未计入合计`)
          return `${scope}；${coverage.join('，')}，不按 0 计入。`
        }
        const coverage: string[] = []
        if (fullCategory && catalog !== null) coverage.push(`目录成员 ${catalog} 个`)
        else if (selected !== null) coverage.push(`本次选中 ${selected} 个`)
        if (comparable !== null) coverage.push(`可比 ${comparable} 个`)
        coverage.push(`不可比 ${nonComparable} 个未计入合计`)
        return `${coverage.join('，')}；不可比成员不按 0 计入。`
      }
      if (fullCategory) return ''
      if (isValueShape) {
        return selected !== null
          ? `合计只代表本次选中的 ${selected} 个商品。`
          : '合计只代表本次选中的商品。'
      }
      // 两期形态没有 member_scope/catalog_member_count；全部可比时覆盖数量已由「覆盖」栏
      // 承担，不再额外生成重复说明。有缺失/不可比时仍走上面的覆盖与限制句。
      return ''
    }
    case 'object_state':
    case 'state':
      return ''
    case 'period':
      return '该依据只说明周期身份，不含商品读数。'
    case 'evidence':
      return ''
    default:
      return ''
  }
}

/**
 * chip/入口的 hover 业务摘要：对象（消歧名）＋指标中文名＋取数方式；不出现 E 编号与 M 代号。
 * missing 项返回中性描述（可核对性由 chip 文案自证）。
 */
export function citeSummary(d: EvidenceDetail, scope: TraceScope): string {
  if (d.locate === 'missing') return '本次未能回查原始记录'
  const parts: string[] = []
  if (d.product_id) {
    const obj = productDisplayName(d.product_id, scope)
    if (obj) parts.push(obj)
  }
  const metric = metricText(d, scope)
  if (metric) parts.push(metric)
  const op = OP_ZH[d.op]
  if (op) parts.push(op)
  return parts.join(' · ')
}

/** 生成业务标题；引用编号与完整引用只展示在技术追溯区。 */
function detailTitle(d: EvidenceDetail, scope: TraceScope): string {
  if (d.locate === 'missing') return '数据依据（暂不可核对）'
  switch (d.op) {
    case 'catalog':
      return '数据依据 · 商品与周期目录'
    case 'period':
      return '数据依据 · 周期引用'
    case 'cohort':
      return '数据依据 · 组合条件筛选结果'
    case 'category_total':
      return d.base || d.target ? '数据依据 · 品类汇总' : '数据依据 · 品类合计'
    case 'set_sum':
      return '数据依据 · 单期集合合计'
    case 'compare':
    case 'window_compare':
      return '数据依据 · 集合对比'
    case 'window':
      return '数据依据 · 日期窗口覆盖'
    case 'day_series':
      return '数据依据 · 连续日读数'
    default: {
      const obj = d.product_id ? productDisplayName(d.product_id, scope) : ''
      if (obj && obj !== d.product_id) return `数据依据 · ${obj}`
      if (obj) return `数据依据 · 商品 ${d.product_id}`
      return '数据依据 · 本次查询记录'
    }
  }
}

/**
 * 投影项 → 详情视图。unknown/缺失一律「本次未提供」或原样状态，不填 0、不猜名字；
 * 集合类只展示服务端口径字段。
 */
export function describeEvidenceDetail(d: EvidenceDetail, scope: TraceScope): DetailView {
  const base: DetailView = {
    title: detailTitle(d, scope),
    lines: [],
    boundary: boundaryText(d),
    rawRef: d.ref,
    handle: d.handle,
    /** 详情标题 hover/读屏用的短编号提示（正文与标题均不直接展示 E 编号） */
    techTrace: techTraceText(d),
    unavailable: d.locate === 'missing',
  }
  if (d.locate === 'missing') {
    base.lines.push(
      { label: '状态', value: '该依据暂不可核对（本次未提供原始行）' },
      { label: '原因', value: d.reason || '引用未在本 run 登记或定位不到' },
    )
    return base
  }
  const lines: DetailLine[] = []
  const addMetric = () => {
    const text = metricText(d, scope)
    if (text) lines.push({ label: '指标', value: text })
  }
  switch (d.op) {
    case 'set_sum': {
      const c = d.collection ?? {}
      addMetric()
      lines.push({ label: '期间', value: periodText(d.period, scope) })
      lines.push({ label: '集合合计', value: readout(d.value, d.unit, d.metric) })
      const coverage: string[] = []
      if (typeof c.selected_n === 'number') coverage.push(`选中 ${c.selected_n}`)
      if (typeof c.comparable_n === 'number') coverage.push(`可用 ${c.comparable_n}`)
      if (typeof c.non_comparable_n === 'number' && c.non_comparable_n > 0) {
        coverage.push(`缺失/不可用 ${c.non_comparable_n}（不按 0 计入）`)
      }
      if (coverage.length) lines.push({ label: '覆盖', value: coverage.join(' · ') })
      const productIds = Array.isArray(c.product_ids)
        ? c.product_ids.filter((id): id is string => typeof id === 'string') : []
      if (productIds.length) {
        lines.push({ label: '本次成员', value: productIds.map((id) => productDisplayName(id, scope)).join('、') })
      } else if (c.selected_n === 0) {
        lines.push({ label: '本次成员', value: '本次没有选中对象' })
      } else if (typeof c.selected_n === 'number') {
        lines.push({ label: '本次成员', value: '名单本次未提供' })
      }
      const byState = c.non_comparable_by_state
      if (byState && typeof byState === 'object' && Object.keys(byState).length) {
        lines.push({ label: '缺失/不可用原因', value: stateCountsText(byState) })
      }
      break
    }
    case 'diff':
    case 'window_diff': {
      addMetric()
      lines.push({ label: '基准周期', value: periodText(d.base, scope) })
      lines.push({ label: '对比周期', value: periodText(d.target, scope) })
      if (d.base || d.target) {
        let baseValue: string
        let targetValue: string
        if (d.base && d.target) {
          [baseValue, targetValue] = pairedReadout(d.base, d.target, d.metric, d.unit, d.state)
        } else {
          baseValue = d.base
            ? readout(d.base.value, d.base.unit ?? d.unit, d.metric, d.base.state, d.state)
            : '本次未提供'
          targetValue = d.target
            ? readout(d.target.value, d.target.unit ?? d.unit, d.metric, d.target.state, d.state)
            : '本次未提供'
        }
        lines.push({ label: '原始读数', value: `${baseValue} → ${targetValue}` })
      }
      if (d.change && (d.change.absolute !== null || d.change.percent !== null)) {
        const percentStatus = unknownStatus(d.change.percent)
        const diffStatus = d.state && d.state !== 'ok'
          ? d.state : percentStatus ?? unknownStatus(d.change.absolute)
        const percentSuffix = typeof d.change.percent === 'number'
          ? `（${percentText(d.change.percent)}）`
          : d.change.percent !== null && percentStatus !== diffStatus
            ? `（${scalarText(d.change.percent, d.state)}）`
            : ''
        lines.push({
          label: '差值',
          value: `${changeReadout(d.change.absolute, d.unit, d.metric, diffStatus, diffStatus)}${percentSuffix}`,
        })
      }
      const status = d.state && d.state !== 'ok'
        ? d.state : unknownStatus(d.change?.percent) ?? unknownStatus(d.change?.absolute)
      if (status) {
        const baseMissing = d.base?.state === 'field_all_missing'
          || unknownStatus(d.base?.value) === 'field_all_missing'
        const targetMissing = d.target?.state === 'field_all_missing'
          || unknownStatus(d.target?.value) === 'field_all_missing'
        const statusText = status === 'field_all_missing' && baseMissing && targetMissing
          ? '基期和对比期都缺少该指标数据，无法比较（不等于 0）'
          : stateLabel(status)
        lines.push({ label: '数据状态', value: statusText })
        const reason = reasonLabel(status, d.reason)
        if (reason) lines.push({ label: '原因', value: reason })
      }
      break
    }
    case 'object':
    case 'window_object': {
      addMetric()
      lines.push({ label: '周期', value: periodText(d.period, scope) })
      const status = d.state && d.state !== 'ok' ? d.state : unknownStatus(d.value)
      lines.push({ label: '原始读数', value: readout(d.value, d.unit, d.metric, status, status) })
      const granularity = granularityText(d.note)
      if (granularity) lines.push({ label: '聚合粒度', value: granularity })
      const denominator = denominatorText(d.denominator)
      if (denominator) lines.push({ label: '分母', value: denominator })
      if (status) {
        lines.push({ label: '数据状态', value: stateLabel(status) })
        const reason = reasonLabel(status, d.reason)
        if (reason) lines.push({ label: '原因', value: reason })
      }
      break
    }
    case 'window': {
      lines.push({ label: '日期窗口', value: periodText(d.period, scope) })
      break
    }
    case 'day_series': {
      //  连续日读数：窗口汇总 + 逐日值（date/value/state 逐字来自响应，缺值不代 0）
      addMetric()
      lines.push({ label: '日期窗口', value: periodText(d.period, scope) })
      const summaryState = d.state && d.state !== 'ok' ? d.state : unknownStatus(d.value)
      lines.push({ label: '窗口汇总', value: readout(d.value, d.unit, d.metric, summaryState, summaryState) })
      const pointStates = d.series?.map((point) => point.state ?? unknownStatus(point.value)) ?? []
      const uniquePointStates = [...new Set(pointStates.filter((state): state is string => !!state && state !== 'ok'))]
      const oneSharedState = uniquePointStates.length === 1
        && (!d.state || d.state === 'ok' || uniquePointStates[0] === d.state)
      if (d.series?.length) {
        lines.push({
          label: '逐日读数',
          value: d.series.map((s) => {
            const pointState = s.state ?? unknownStatus(s.value)
            const state = pointState && pointState !== 'ok' && !oneSharedState
              ? `（${stateLabel(pointState)}${reasonLabel(pointState, s.reason) ? `：${reasonLabel(pointState, s.reason)}` : ''}）` : ''
            return `${s.date ?? '本次未提供'} ${readout(s.value, d.unit, d.metric, pointState, pointState)}${state}`
          }).join('；'),
        })
      }
      const sharedState = summaryState ?? (oneSharedState ? uniquePointStates[0] : null)
      if (sharedState) lines.push({ label: '数据状态', value: stateLabel(sharedState) })
      break
    }
    case 'state': {
      lines.push({ label: '周期', value: periodText(d.period, scope) })
      if (d.state) {
        lines.push({ label: '记录状态', value: d.state === 'ok' ? '记录正常' : stateLabel(d.state) })
        if (d.state !== 'ok') {
          const reason = reasonLabel(d.state, d.reason)
          if (reason) lines.push({ label: '原因', value: reason })
        }
      }
      break
    }
    case 'compare':
    case 'window_compare': {
      const c = d.collection ?? {}
      addMetric()
      lines.push({ label: '周期', value: periodText(d.period, scope) })
      if (c.kind === 'median') {
        const rawMedianState = String(c.median_state ?? '')
        const medianState = unknownStatus(rawMedianState) ?? rawMedianState
        const median = (c.median ?? null) as string | number | null
        const medianValue = median === null && medianState && medianState !== 'ok'
          ? stateLabel(medianState)
          : readout(median, d.unit, d.metric, medianState, medianState)
        lines.push({ label: '集合中位数', value: medianValue })
        if (median !== null && medianState && medianState !== 'ok') {
          lines.push({ label: '基准状态', value: stateLabel(medianState) })
        }
      }
      const counts: string[] = []
      if (typeof c.selected_n === 'number') counts.push(`选中 ${c.selected_n}`)
      if (typeof c.usable_n === 'number') counts.push(`可用 ${c.usable_n}`)
      if (typeof c.missing_n === 'number' && c.missing_n > 0) counts.push(`缺失 ${c.missing_n}`)
      if (counts.length) lines.push({ label: '集合范围', value: counts.join(' · ') })
      if (c.kind === 'median') {
        const grp: string[] = []
        if (typeof c.high_n === 'number' && c.high_n > 0) grp.push(`高于中位数 ${c.high_n}`)
        if (typeof c.equal_n === 'number' && c.equal_n > 0) grp.push(`持平 ${c.equal_n}`)
        if (typeof c.low_n === 'number' && c.low_n > 0) grp.push(`低于中位数 ${c.low_n}`)
        if (grp.length) lines.push({ label: '分组', value: grp.join(' · ') })
      }
      if (c.kind === 'rank') {
        const ranking = Array.isArray(c.ranking)
          ? c.ranking as { rank: number | null; product_id: string | null; value: string | number | null }[]
          : null
        if (ranking?.length) {
          lines.push({
            label: '排名',
            value: ranking.map((row) => {
              const subject = productDisplayName(row.product_id, scope)
              const rank = row.rank === null ? '名次本次未提供' : `第 ${row.rank} 名`
              return `${rank} ${subject}：${readout(row.value, d.unit, d.metric)}`
            }).join('；'),
          })
        } else if (ranking) {
          lines.push({
            label: '排名',
            value: c.usable_n === 0 ? '本次没有可排名的读数' : '本次未返回可用排名行',
          })
        } else if (typeof c.ranking_n === 'number' && c.ranking_n > 0) {
          lines.push({ label: '排名', value: '排名明细本次未提供' })
        }

        const missing = Array.isArray(c.missing_group)
          ? c.missing_group as { product_id: string | null; value: string | number | null; state: string | null; reason: string | null }[]
          : null
        if (missing?.length) {
          lines.push({
            label: '缺失成员',
            value: missing.map((member) =>
              `${productDisplayName(member.product_id, scope)}：${memberReadingText(member.value, d.unit, d.metric, member.state, member.reason)}`,
            ).join('；'),
          })
        } else if (missing && typeof c.missing_n === 'number' && c.missing_n > 0) {
          lines.push({ label: '缺失成员', value: '缺失成员明细本次未提供' })
        }

        // 排名与缺失成员已覆盖选中范围时不再重复名单；只有确有未覆盖成员时才补这一行。
        const objects = Array.isArray(c.objects) ? c.objects as string[] : null
        if (objects) {
          const rankedIds = new Set((ranking ?? [])
            .map((row) => row.product_id)
            .filter((id): id is string => typeof id === 'string'))
          const missingIds = new Set((missing ?? [])
            .map((member) => member.product_id)
            .filter((id): id is string => typeof id === 'string'))
          const uncovered = objects.filter((id) => !rankedIds.has(id) && !missingIds.has(id))
          if (uncovered.length) {
            lines.push({
              label: '未列入排名或缺失的选中商品',
              value: uncovered.map((id) => productDisplayName(id, scope)).join('、'),
            })
          } else if (objects.length === 0 && c.selected_n === 0) {
            lines.push({ label: '本次选中商品', value: '本次没有选中对象' })
          }
        } else if (typeof c.selected_n === 'number' && c.selected_n > 0) {
          lines.push({ label: '本次选中商品', value: '成员名单本次未提供' })
        }
      }
      // 中位数/排名的定义已由结果行（集合中位数、分组、名次）表达清楚；不再重复底层
      // 计算规则或固定基准声明。未知比较类型仍保留服务端口径说明。
      if (d.note && c.kind !== 'rank' && c.kind !== 'median') {
        lines.push({ label: '口径说明', value: d.note })
      }
      break
    }
    case 'compare_row':
    case 'window_compare_row': {
      if (d.membership?.group_member) {
        const group = d.membership.group
        if (group) {
          lines.push({ label: '比较结果', value: `${metricText(d, scope)}${COMPARE_GROUP_ZH[group]}` })
        } else {
          lines.push({ label: '比较结果', value: '该商品属于本次分组结果，具体分组本次未提供' })
        }
      } else {
        lines.push({ label: '名次', value: d.membership?.rank != null ? `第 ${String(d.membership.rank)} 名` : '本次未提供' })
        lines.push({ label: '原始读数', value: readout(d.value, d.unit, d.metric) })
      }
      break
    }
    case 'compare_missing':
    case 'window_compare_missing': {
      const status = d.state ?? unknownStatus(d.value)
      if (typeof d.value === 'number') {
        lines.push({ label: '原始读数', value: readout(d.value, d.unit, d.metric) })
      }
      if (status) lines.push({ label: '数据状态', value: stateLabel(status) })
      const reason = reasonLabel(status, d.reason)
      if (reason) lines.push({ label: '原因', value: reason })
      break
    }
    case 'object_state': {
      if (d.state) {
        lines.push({ label: '身份状态', value: d.state === 'ok' ? '身份一致' : stateLabel(d.state) })
      }
      const changes = crossPeriodChanges(d)
      // 具体跨期变化已逐字段给出时，不再重复「记录状态跨期发生变化」的概括；
      // 属性漂移仍需保留「需复核」提醒，身份/记录冲突也不在此过滤。
      const checkStates = (d.states ?? [])
        .filter((state) => state !== d.state)
        .filter((state) => !(state === 'state_change' && changes))
      if (checkStates.length) {
        lines.push({ label: '核对结果', value: checkStates.map(objectStateLabel).join('；') })
      }
      if (d.base) lines.push({ label: '基准周期', value: periodText(d.base, scope) })
      if (d.target) lines.push({ label: '对比周期', value: periodText(d.target, scope) })
      if (changes) lines.push({ label: '跨期变化', value: changes })
      break
    }
    case 'cohort': {
      //  集合级筛选引用：条件/期间/全部命中与未知成员、计数随引用整体登记
      const c = d.collection ?? {}
      const scopeCount = typeof c.scope_count === 'number' ? c.scope_count : null
      const scopeText = c.scope_kind === 'product_ids'
        ? `指定商品集合（${scopeCount ?? '—'} 个）`
        : c.scope_kind === 'category'
          ? `指定品类商品（${scopeCount ?? '—'} 个）`
          : c.scope_kind === 'benchmark_all'
            ? `全部商品（${scopeCount ?? '—'} 个）`
            : `本次筛选范围（${scopeCount ?? '—'} 个商品）`
      lines.push({ label: '筛选范围', value: scopeText })
      const periodLine = d.base || d.target
        ? `${periodText(d.base, scope)} → ${periodText(d.target, scope)}`
        : periodText(d.period, scope)
      if (periodLine && periodLine !== '本次未提供') lines.push({ label: '期间', value: periodLine })
      if (Array.isArray(c.conditions) && c.conditions.length) {
        lines.push({
          label: '筛选条件（同时满足）',
          value: (c.conditions as { metric: string | null; field: string | null; operator: string | null; value: string | number | null }[])
            .map((cond) => cohortConditionText(cond, scope))
            .join('；'),
        })
      }
      const counts: string[] = []
      if (typeof c.match_count === 'number') counts.push(`命中 ${c.match_count}`)
      if (typeof c.nonmatch_count === 'number') counts.push(`未命中 ${c.nonmatch_count}`)
      if (typeof c.predicate_unknown_count === 'number' && c.predicate_unknown_count > 0) {
        counts.push(`无法判定 ${c.predicate_unknown_count}`)
      }
      if (counts.length) lines.push({ label: '结果', value: counts.join(' · ') })
      if (typeof c.confirmed_share_percent === 'number' && scopeCount) {
        const label = (typeof c.predicate_unknown_count === 'number' && c.predicate_unknown_count > 0)
          ? '已确认命中占比'
          : '命中占比'
        lines.push({ label, value: `${percentText(c.confirmed_share_percent)}（分母：本次筛选的 ${scopeCount} 个商品）` })
      }
      if (Array.isArray(c.match_ids) && c.match_ids.length) {
        lines.push({
          label: '命中商品',
          value: (c.match_ids as string[]).map((id) => productDisplayName(id, scope)).join('、'),
        })
      }
      if (Array.isArray(c.unknown_members) && c.unknown_members.length) {
        lines.push({
          label: '无法判定商品（不计入命中）',
          value: (c.unknown_members as { product_id: string | null; reasons: string[] }[])
            .map((u) => `${productDisplayName(u.product_id, scope)}（${u.reasons.map(stateLabel).join('；') || '原因未登记'}）`)
            .join('、'),
        })
      }
      break
    }
    case 'catalog': {
      const c = d.collection ?? {}
      const bits: string[] = []
      if (typeof c.product_n === 'number') bits.push(`商品 ${c.product_n} 个`)
      if (typeof c.period_n === 'number') bits.push(`周期 ${c.period_n} 个`)
      lines.push({ label: '目录规模', value: bits.join(' · ') || '本次未提供' })
      break
    }
    case 'category_total': {
      //  品类汇总集合级引用：数值/单位/两期/覆盖与不可比分类随引用整体登记（Data 原值）
      // value 单期品类合计（aggregate_kind=category_total / category_selected_total）
      // 走同一键位——期间=该期，值=该组合计，覆盖与成员贡献原样取 Data，不复算。
      const c = d.collection ?? {}
      const isValueShape = c.base_sum == null && c.target_sum == null
        && (c.aggregate_kind === 'category_total' || c.aggregate_kind === 'category_selected_total')
      addMetric()
      if (c.category) lines.push({ label: '品类', value: String(c.category) })
      if (isValueShape) {
        lines.push({ label: '期间', value: periodText(d.period, scope) })
        lines.push({ label: '品类合计', value: readout(c.sum ?? null, d.unit, d.metric) })
        if (c.member_scope === 'full_category') {
          lines.push({ label: '成员范围', value: '目录完整品类成员' })
        } else if (c.member_scope === 'selected_members_only') {
          lines.push({ label: '成员范围', value: '本次选中成员' })
        }
      } else {
        lines.push({
          label: '期间',
          value: d.base || d.target
            ? `${periodText(d.base, scope)} → ${periodText(d.target, scope)}`
            : '本次未提供',
        })
        const baseSum = typeof c.base_sum === 'string' || typeof c.base_sum === 'number' ? c.base_sum : null
        const targetSum = typeof c.target_sum === 'string' || typeof c.target_sum === 'number' ? c.target_sum : null
        const sums = `${readout(baseSum, d.unit, d.metric)} → ${readout(targetSum, d.unit, d.metric)}`
        lines.push({ label: '品类合计', value: sums })
      }
      const absoluteChange = typeof c.absolute_change === 'string' || typeof c.absolute_change === 'number'
        ? c.absolute_change : null
      if (!isValueShape) {
        if (absoluteChange !== null) {
          const pct = c.percent_change
          const pctText = typeof pct === 'number'
            ? `（${percentText(pct)}）`
            : (typeof pct === 'string' ? `（${scalarText(pct)}）` : '')
          lines.push({
            label: '合计变化',
            value: `${changeReadout(absoluteChange, d.unit, d.metric)}${pctText}`,
          })
        } else if (typeof c.percent_change === 'string') {
          lines.push({ label: '合计变化', value: scalarText(String(c.percent_change)) })
        }
      }
      const cover: string[] = []
      if (typeof c.selected_n === 'number') cover.push(`选中 ${c.selected_n}`)
      if (typeof c.comparable_n === 'number') cover.push(`可比 ${c.comparable_n}`)
      if (typeof c.non_comparable_n === 'number' && c.non_comparable_n > 0) {
        cover.push(`不可比 ${c.non_comparable_n}`)
      }
      if (typeof c.catalog_member_count === 'number') {
        cover.push(`目录成员 ${c.catalog_member_count}`)
      }
      if (cover.length) lines.push({ label: '覆盖', value: cover.join(' · ') })
      const byState = c.non_comparable_by_state
      if (byState && typeof byState === 'object' && Object.keys(byState).length) {
        lines.push({ label: '不可比原因', value: stateCountsText(byState) })
      }
      // 主要贡献商品（同一响应已算好的贡献，不在此复算比例）；只列占比明确可用的成员。
      const contributors = Array.isArray(c.contributors)
        ? (c.contributors as { product_id?: unknown; value?: unknown; share_percent?: unknown }[])
          .filter((x) => x && typeof x.product_id === 'string' && typeof x.share_percent === 'number')
        : []
      if (contributors.length) {
        const top = contributors.slice(0, 5)
        lines.push({
          label: '主要贡献商品',
          value: top
            .map((x) => {
              const name = productDisplayName(String(x.product_id), scope)
              const share = typeof x.share_percent === 'number' ? percentText(x.share_percent) : ''
              return `${name}${share ? `（${share}）` : ''}`
            })
            .join('、'),
        })
      }
      break
    }
    case 'evidence': {
      if (d.metric || d.metric_name) addMetric()
      lines.push({ label: '周期', value: periodText(d.period, scope) })
      if (d.metric_rows?.length) {
        lines.push({
          label: '原始读数',
          value: d.metric_rows
            .map((r) => {
              const name = metricNameText(r.metric, r.metric_name, scope) || r.metric
              return `${name}：${readout(r.value, r.unit, r.metric, r.state, d.state)}`
            })
            .join('；'),
        })
      } else if (d.metric || d.metric_name) {
        lines.push({ label: '原始读数', value: readout(d.value, d.unit, d.metric, d.state, d.state) })
      }
      if (d.state && d.state !== 'ok') {
        lines.push({ label: '数据状态', value: stateLabel(d.state) })
        const reason = reasonLabel(d.state, d.reason)
        if (reason) lines.push({ label: '原因', value: reason })
      }
      break
    }
    case 'period': {
      lines.push({ label: '周期', value: periodText(d.period, scope) })
      break
    }
    default:
      break
  }
  base.lines = lines
  return base
}

/** 正文 [E#] 与证据清单行 → 可点击判定：本 run 是否有该引用的详情项（含 missing=可看原因）。 */
export function evidenceByHandle(details: EvidenceDetail[] | null): Map<string, EvidenceDetail> {
  const m = new Map<string, EvidenceDetail>()
  for (const d of details ?? []) {
    if (d.handle) m.set(d.handle, d)
    m.set(d.ref, d)     // canonical 也可定位（依据详情的「原始引用」）
  }
  return m
}

/**
 *  轻量引用编号单点：本次已登记依据的稳定序号（1 起，按服务端 accepted 顺序）。
 * `handle`（E#）与 canonical `ref` 两种写法都指向同一编号——正文、数据行与证据清单引用同一
 * 证据时编号一致；未登记的引用不在表内（消费方保持明确的「依据暂不可核对」状态，不伪造编号）。
 * 页面与证据面板共用本函数，不建第二套编号规则。
 */
export function citeNumberMap(details: EvidenceDetail[] | null): Map<string, number> {
  const m = new Map<string, number>()
  ;(details ?? []).forEach((d, i) => {
    if (d.handle) m.set(d.handle, i + 1)
    if (d.ref) m.set(d.ref, i + 1)
  })
  return m
}
