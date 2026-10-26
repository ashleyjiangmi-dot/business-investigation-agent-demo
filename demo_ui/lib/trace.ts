import type { CallParams } from './trace-filter'

/** 周期目录行：入口上下文运行时下发（ 跨周期双下拉与周期标签展示共用，不另建目录）。 */
export type ScopePeriod = {
  period_id: string
  period_label: string
  period_start_date: string
  period_end_date: string
  length_days?: number
}

/**
 * 周期目录的时间序（单点）：以目录真实日期区间排序，出现顺序与 ID 字符串序都不可信
 * （opview 目录尾部才是 p1/p2）。入口下拉选项、默认值与展示标签共用这一条口径。
 */
export function periodsInTimeOrder(periods: ScopePeriod[]): ScopePeriod[] {
  return [...periods].sort((a, b) =>
    a.period_start_date.localeCompare(b.period_start_date)
    || a.period_end_date.localeCompare(b.period_end_date))
}

/** 运行时下发的一次性目录投影（页面不维护第二套商品目录，仓库规则 5）。 */
export type TraceScope = {
  products: {
    product_id: string
    product_name: string
    category: string
    /** 同名商品消歧标签（服务端单点现算，同名＝「名称（品牌）」）；唯一名时即名称 */
    brand?: string
    scope_label?: string
  }[]
  period_choices: Record<string, string[]>
  /** 完整周期目录；缺失时使用选项标签或原始 ID。 */
  periods?: ScopePeriod[]
  /** 服务端下发的指标代号与中文名映射。 */
  metrics: Record<string, string>
}

export const EMPTY_SCOPE: TraceScope = { products: [], period_choices: {}, periods: [], metrics: {} }

/** planned=模型已排入本轮计划但调用尚未开始；running=CALL start 已到；done/failed=CALL success 终账 */
export type StepState = 'planned' | 'running' | 'done' | 'failed'

export type Step = {
  key: string
  tool: string
  text: string
  state: StepState
  /** 失败时的工具状态码（如 period_rejected）；成功为 null */
  detail: string | null
  elapsed: number | null
  /** 空串=该步骤尚无参数可比（CALL start 帧不带参数） */
  paramsJson: string
}

export type Trace = {
  steps: Step[]
}

export const EMPTY_TRACE: Trace = { steps: [] }

const join = (parts: (string | undefined | null)[]) => parts.filter(Boolean).join(' · ')
const list = (v: unknown) => (Array.isArray(v) ? v.map(String).join('/') : v == null ? '' : String(v))
const metricList = (v: unknown, scope: TraceScope) =>
  (Array.isArray(v) ? v : v == null ? [] : [v])
    .map((metric) => metricLabel(metric, scope))
    .filter(Boolean)
    .join(' / ')
const strip = (label: string) => label.replace(/（[^）]*）/g, '').trim()

function periodRange(base: unknown, target: unknown, scope: TraceScope): string {
  const baseLabel = periodLabel(base, scope)
  const targetLabel = periodLabel(target, scope)
  return baseLabel && targetLabel ? `${baseLabel} → ${targetLabel}` : baseLabel || targetLabel
}

type PeriodRef = { periodId?: string; startDate?: string; endDate?: string }
type TimeSelection = { text: string; refs: PeriodRef[]; specified: boolean; slots?: PeriodRef[][] }

function rangeText(start: unknown, end: unknown): string {
  if (typeof start !== 'string' || typeof end !== 'string') return ''
  return start === end ? start : `${start} 至 ${end}`
}

function periodRef(periodId: string, scope?: TraceScope): PeriodRef {
  const row = scope?.periods?.find((period) => period.period_id === periodId)
  return {
    periodId,
    ...(row?.period_start_date ? { startDate: row.period_start_date } : {}),
    ...(row?.period_end_date ? { endDate: row.period_end_date } : {}),
  }
}

function dateRef(startDate: string, endDate: string, scope?: TraceScope): PeriodRef[] {
  const exactPeriods = (scope?.periods ?? []).filter(
    (period) => period.period_start_date === startDate && period.period_end_date === endDate,
  )
  return exactPeriods.length
    ? exactPeriods.map((period) => periodRef(period.period_id, scope))
    : [{ startDate, endDate }]
}

function timeSlot(slot: unknown, scope: TraceScope): TimeSelection {
  if (!slot || typeof slot !== 'object' || Array.isArray(slot)) {
    return { text: '', refs: [], specified: false }
  }
  const value = slot as Record<string, unknown>
  const specified = Object.keys(value).length > 0
  const ordered = periodsInTimeOrder(scope.periods ?? [])
  const selectedPeriod = (index: number, fallback: string): TimeSelection => {
    const period = ordered[index]
    return period
      ? { text: periodLabel(period.period_id, scope), refs: [periodRef(period.period_id, scope)], specified: true }
      : { text: fallback, refs: [], specified: true }
  }
  if (value.which === 'latest') return selectedPeriod(ordered.length - 1, '最新周期')
  if (value.which === 'previous') return selectedPeriod(ordered.length - 2, '上一周期')
  if (value.which === 'two_ago') return selectedPeriod(ordered.length - 3, '上上周期')
  if (typeof value.offset === 'number' && Number.isInteger(value.offset) && value.offset <= 0) {
    const n = Math.abs(value.offset)
    return selectedPeriod(ordered.length - 1 - n, n === 0 ? '最新周期' : `向前回溯 ${n} 期`)
  }
  if (typeof value.date === 'string') {
    return { text: value.date, refs: dateRef(value.date, value.date, scope), specified: true }
  }
  if (value.date_range && typeof value.date_range === 'object' && !Array.isArray(value.date_range)) {
    const range = value.date_range as Record<string, unknown>
    const start = typeof range.start_date === 'string' ? range.start_date : ''
    const end = typeof range.end_date === 'string' ? range.end_date : ''
    return { text: rangeText(start, end), refs: start && end ? dateRef(start, end, scope) : [], specified: true }
  }
  if (typeof value.days_ago === 'number') {
    return { text: value.days_ago === 0 ? '最近完整日' : `${value.days_ago} 天前`, refs: [], specified: true }
  }
  if (typeof value.weeks_ago === 'number') {
    return { text: value.weeks_ago === 0 ? '最近完整周' : `${value.weeks_ago} 周前同一日`, refs: [], specified: true }
  }
  if (typeof value.last_days === 'number') {
    return { text: `最近 ${value.last_days} 天`, refs: [], specified: true }
  }
  if (typeof value.month === 'string') {
    const label = value.month === 'this' ? '本月' : value.month === 'last' ? '上月' : value.month.replace('-', '年') + '月'
    return { text: label, refs: [], specified: true }
  }
  if (typeof value.month_to_date === 'string') {
    const label = value.month_to_date === 'this' ? '本月至今' : value.month_to_date === 'last' ? '上月同期' : value.month_to_date.replace('-', '年') + '月同期'
    return { text: label, refs: [], specified: true }
  }
  return { text: '', refs: [], specified }
}

function singleTime(params: Record<string, unknown>, scope: TraceScope): TimeSelection {
  const time = params.time && typeof params.time === 'object' && !Array.isArray(params.time)
    ? params.time as Record<string, unknown>
    : null
  if (time && time.period !== undefined) return timeSlot(time.period, scope)
  if (typeof params.period_id === 'string') {
    return { text: periodLabel(params.period_id, scope), refs: [periodRef(params.period_id, scope)], specified: true }
  }
  const window = params.window && typeof params.window === 'object' && !Array.isArray(params.window)
    ? params.window as Record<string, unknown>
    : null
  if (window) {
    const start = typeof window.start_date === 'string' ? window.start_date : ''
    const end = typeof window.end_date === 'string' ? window.end_date : ''
    return { text: rangeText(start, end), refs: start && end ? dateRef(start, end, scope) : [], specified: true }
  }
  return { text: '', refs: [], specified: false }
}

function pairedTime(params: Record<string, unknown>, scope: TraceScope): TimeSelection {
  const time = params.time && typeof params.time === 'object' && !Array.isArray(params.time)
    ? params.time as Record<string, unknown>
    : null
  if (time && (time.base !== undefined || time.target !== undefined)) {
    const base = timeSlot(time.base, scope)
    const target = timeSlot(time.target, scope)
    return {
      text: base.text && target.text ? `${base.text} → ${target.text}` : base.text || target.text,
      refs: [...base.refs, ...target.refs],
      specified: base.specified || target.specified,
      slots: [base.refs, target.refs],
    }
  }
  const baseId = typeof params.base_period_id === 'string' ? params.base_period_id : ''
  const targetId = typeof params.target_period_id === 'string' ? params.target_period_id : ''
  if (baseId || targetId) {
    return {
      text: periodRange(baseId, targetId, scope),
      refs: [baseId, targetId].filter(Boolean).map((id) => periodRef(id, scope)),
      specified: true,
      slots: [baseId ? [periodRef(baseId, scope)] : [], targetId ? [periodRef(targetId, scope)] : []],
    }
  }
  const baseWindow = params.base_window && typeof params.base_window === 'object' && !Array.isArray(params.base_window)
    ? params.base_window as Record<string, unknown>
    : null
  const targetWindow = params.target_window && typeof params.target_window === 'object' && !Array.isArray(params.target_window)
    ? params.target_window as Record<string, unknown>
    : null
  const baseStart = typeof baseWindow?.start_date === 'string' ? baseWindow.start_date : ''
  const baseEnd = typeof baseWindow?.end_date === 'string' ? baseWindow.end_date : ''
  const targetStart = typeof targetWindow?.start_date === 'string' ? targetWindow.start_date : ''
  const targetEnd = typeof targetWindow?.end_date === 'string' ? targetWindow.end_date : ''
  if (baseWindow || targetWindow) {
    return {
      text: [rangeText(baseStart, baseEnd), rangeText(targetStart, targetEnd)].filter(Boolean).join(' → '),
      refs: [
        ...(baseStart && baseEnd ? dateRef(baseStart, baseEnd, scope) : []),
        ...(targetStart && targetEnd ? dateRef(targetStart, targetEnd, scope) : []),
      ],
      specified: true,
      slots: [
        baseStart && baseEnd ? dateRef(baseStart, baseEnd, scope) : [],
        targetStart && targetEnd ? dateRef(targetStart, targetEnd, scope) : [],
      ],
    }
  }
  return { text: '', refs: [], specified: false }
}

/** Current query digests may be wrapped as { query: { op, ... } }; knowledge_search keeps
 * a string-valued query and therefore remains on its outer parameter object.
 * 三个 MCP 查询工具的步骤参数是**扁平的数据层请求**（无 query 包裹，含 op），因此
 * 与 oagent_query 共用同一套业务文案；只有 oagent_query 需要拆 query 包裹。 */
function queryParams(tool: string, params: CallParams): Record<string, unknown> {
  const outer = (params ?? {}) as Record<string, unknown>
  const nested = outer.query
  return tool === 'oagent_query' && nested && typeof nested === 'object' && !Array.isArray(nested)
    ? (nested as Record<string, unknown>)
    : outer
}

/** 取数工具集合：步骤文案按 op 生成，不按工具名给泛化文案。：新增筛选工具。 */
const QUERY_TOOLS = new Set([
  'oagent_query',
  'get_metric_values',
  'compare_periods',
  'compare_product_set',
  'filter_products',
])

/**
 * 周期人读标签（：**日期优先**）。
 * 目录命中（运行时下发的真实日期区间）→ 直接给 `2026-05-14~2026-05-20`：结果区里两期比较
 * 统一用真实日期说话，不再重复「（模拟）」「上一周期（模拟）」等无必要限定。
 * 目录查不到时的回退顺序：单期选择标签（「本周期/上一周期」的人话说法，旧底件零 periods 仍可跑）
 * → 原样给 id（不猜名字、不编日期）。
 */
export function periodLabel(pid: unknown, scope: TraceScope): string {
  if (typeof pid !== 'string') return ''
  const row = (scope.periods ?? []).find((p) => String(p.period_id) === pid)
  if (row && row.period_start_date && row.period_end_date) {
    return `${row.period_start_date}~${row.period_end_date}`
  }
  const label = Object.entries(scope.period_choices).find(
    ([, ids]) => ids.length === 1 && ids[0] === pid,
  )?.[0]
  if (label) return strip(label)
  if (row) {
    const name = strip(row.period_label) || pid
    return row.period_start_date && row.period_end_date
      ? `${name}（${row.period_start_date}~${row.period_end_date}）`
      : name
  }
  return pid
}

/** 单个商品名；目录里没有就原样给 id（不猜名字）。同名商品用下发消歧标签（含品牌）。 */
export function productName(id: unknown, scope: TraceScope): string {
  const pid = String(id ?? '')
  if (!pid) return ''
  const p = scope.products.find((x) => x.product_id === pid)
  return p ? p.scope_label ?? p.product_name : pid
}

/** 指标代号→中文名；映射缺失时原样给代号（不猜名字）。 */
export function metricLabel(m: unknown, scope: TraceScope): string {
  const id = String(m ?? '')
  if (!id) return ''
  const name = scope.metrics?.[id]
  return name ? `${name} ${id}` : id
}

/** 对象范围：全池 / 同品类 / 单个商品名 / N 个商品（无法归类时给数量中性描述）。 */
export function objLabel(ids: unknown, scope: TraceScope): string {
  const items = Array.isArray(ids) ? ids.map(String) : []
  if (!items.length) return ''
  const known = items
    .map((id) => scope.products.find((p) => p.product_id === id))
    .filter((p): p is TraceScope['products'][number] => Boolean(p))
  if (known.length !== items.length) return `${items.length} 个商品`
  if (scope.products.length && items.length >= scope.products.length) return '全池'
  if (items.length === 1) return known[0].scope_label ?? known[0].product_name
  const categories = new Set(known.map((p) => p.category))
  return categories.size === 1 ? `同品类（${[...categories][0]}）` : `${items.length} 个商品`
}

function scopeLabel(params: Record<string, unknown>, scope: TraceScope): string {
  if (params.product_ids !== undefined) return objLabel(params.product_ids, scope)
  if (params.product_id !== undefined) return objLabel([params.product_id], scope)
  // cohort_filter 的范围在 scope 对象里（kind=benchmark_all | product_ids）
  // 范围形态与查询工具统一（benchmark_all | category | product_ids）
  const sc = params.scope
  if (sc && typeof sc === 'object' && !Array.isArray(sc)) {
    const s = sc as Record<string, unknown>
    if (s.kind === 'benchmark_all') return '全池'
    if (s.kind === 'category' && Array.isArray(s.categories)) {
      const cats = s.categories.map(String)
      return cats.length ? `同品类（${cats.join('、')}）` : '同品类'
    }
    if (s.kind === 'product_ids' && Array.isArray(s.product_ids)) return objLabel(s.product_ids, scope)
  }
  return ''
}

/** 简单确定性映射；无法归类即给中性描述，不让模型编「为什么这么查」。 */
export function describeCall(tool: string, params: CallParams, scope: TraceScope): string {
  if (tool === 'knowledge_search') return '检索经营方法'
  const p = queryParams(tool, params)
  const obj = scopeLabel(p, scope)
  if (tool === 'oagent_evidence') return join(['核查数据覆盖与记录状态', obj, periodLabel(p.period_id, scope)])
  if (tool === 'oagent_specialist') return '委派专项经营调查'
  if (!QUERY_TOOLS.has(tool)) return `调用 ${tool}`
  switch (p.op) {
    case 'diff': {
      // MCP compare_periods 用 metrics[]（可多指标）；旧单指标 metric 仍兼容。
      return join(['对比两期核心指标', metricList(p.metrics ?? p.metric, scope), obj,
        pairedTime(p, scope).text])
    }
    case 'value':
      return join(['查询指标值', metricList(p.metrics ?? p.metric, scope), obj,
        singleTime(p, scope).text])
    case 'set_compare':
      return join(['对比表现', metricList(p.metric, scope), obj, singleTime(p, scope).text])
    case 'states':
      return join(['核查两期记录状态', obj,
        pairedTime(p, scope).text])
    case 'daily_series': {
      return join(['查看连续每日读数', metricList(p.metrics ?? p.metric, scope), obj,
        singleTime(p, scope).text])
    }
    case 'catalog':
      return '读取商品目录'
    case 'cohort_filter': {
      // 组合条件筛选——条件明细（指标/阈值）在白名单参数里逐字可见，此处只给
      // 确定性概览（条件数/范围/期间），不解释业务含义
      const conds = Array.isArray(p.conditions) ? p.conditions.length : 0
      return join(['组合条件筛选',
        conds ? `${conds} 个条件` : '',
        obj || '全池',
        singleTime(p, scope).text || pairedTime(p, scope).text])
    }
    default:
      return join(['查询', list(p.op), obj])
  }
}

function newStep(
  key: string,
  tool: string,
  params: CallParams,
  scope: TraceScope,
  state: StepState = 'planned',
): Step {
  return {
    key,
    tool,
    text: describeCall(tool, params, scope),
    state,
    detail: null,
    elapsed: null,
    paramsJson: params == null ? '' : JSON.stringify(params),
  }
}

/**
 * 将服务端步骤事件映射为时间线。
 * step_started 表示实际开始执行，step_finished 决定完成或失败状态。
 * 不从模型计划推断执行情况，也不展示思维链。
 */
export function applyTraceEvent(
  trace: Trace,
  ev: { event?: string; data?: unknown },
  scope: TraceScope,
): Trace {
  if (ev.event !== 'step_started' && ev.event !== 'step_finished') return trace
  const d = (ev.data ?? {}) as Record<string, any>
  const steps = [...trace.steps]

  if (ev.event === 'step_started') {
    const tool = String(d.label ?? '')
    let params: CallParams = null
    try {
      params = typeof d.params_digest === 'string' && d.params_digest ? JSON.parse(d.params_digest) : null
    } catch {
      params = null
    }
    steps.push(newStep(String(d.step_id ?? ''), tool, params, scope, 'running'))
    return { steps }
  }

  const idx = steps.findIndex((s) => s.key === String(d.step_id ?? ''))
  const done = newStep(
    String(d.step_id ?? ''),
    idx >= 0 ? steps[idx].tool : '',
    idx >= 0 ? (JSON.parse(steps[idx].paramsJson || 'null') as CallParams) : null,
    scope,
  )
  done.state = d.status === 'success' ? 'done' : 'failed'
  done.detail = done.state === 'failed' && d.result_code ? String(d.result_code) : null
  done.elapsed = typeof d.elapsed_ms === 'number' ? d.elapsed_ms / 1000 : null
  if (idx >= 0) {
    steps[idx] = { ...done, key: steps[idx].key }
  } else {
    steps.push(done) // start 帧丢失时按终账补一条（不猜过程，文案中性）
  }
  return { steps }
}

/**
 * 取数族：一次调用与一条证据 handle 各自属于哪一类取数。
 * 两侧族都已知且不同时不算匹配（否则 `diff` 证据会挂到恰好查过同一指标的 `value` 步骤上）；
 * 族未知（如 oagent_query 之外的工具、未收录的 op）时不加这条约束，退回指标/对象/周期判断。
 * `pev:` 对应 oagent_evidence（服务端 oagent_ctx 文档：单期取证响应登记）。
 */
export const OP_FAMILY: Record<string, string> = {
  value: 'single',
  object: 'single',
  diff: 'diff',
  states: 'state',
  state: 'state',
  object_state: 'state',
  set_compare: 'set',
  compare: 'set',
  compare_row: 'set',
  catalog: 'catalog',
  // cohort_filter 步骤与其集合级引用（resolve 详情 op='cohort'）同族
  cohort_filter: 'cohort',
  cohort: 'cohort',
  //  日期窗口：步骤 op 与引用 kind 同族（window_object←value、window_diff←diff、
  // window_compare*←set_compare、day_series←daily_series），使证据行仍能挂回对应调用。
  daily_series: 'single',
  window: 'single',
  window_object: 'single',
  window_diff: 'diff',
  window_compare: 'set',
  window_compare_row: 'set',
  window_compare_missing: 'set',
  day_series: 'single',
}

function stepFamily(step: Step, p: Record<string, any>): string {
  if (step.tool === 'oagent_evidence') return 'evidence'
  return OP_FAMILY[String(p.op ?? '')] ?? ''
}

function selectedPeriods(params: Record<string, unknown>, scope: TraceScope): TimeSelection {
  const time = params.time && typeof params.time === 'object' && !Array.isArray(params.time)
    ? params.time as Record<string, unknown>
    : null
  if (time && (time.base !== undefined || time.target !== undefined)) return pairedTime(params, scope)
  if (time && time.period !== undefined) return singleTime(params, scope)
  if (typeof params.period_id === 'string' || params.window !== undefined) return singleTime(params, scope)
  if (typeof params.base_period_id === 'string' || typeof params.target_period_id === 'string'
    || params.base_window !== undefined || params.target_window !== undefined) return pairedTime(params, scope)
  return { text: '', refs: [], specified: false }
}

function samePeriod(a: PeriodRef, b: PeriodRef): boolean {
  if (a.periodId && b.periodId && a.periodId === b.periodId) return true
  return Boolean(a.startDate && a.endDate && b.startDate && b.endDate
    && a.startDate === b.startDate && a.endDate === b.endDate)
}

/** 轻量匹配：证据行的取数族/指标/对象/周期落在哪一次调用上（匹配不到由调用方标「—」，不猜）。 */
export function stepMatchesEvidence(
  step: Step,
  ev: {
    metric: string
    productId: string
    periodId?: string
    periods?: PeriodRef[]
    op?: string
    ns?: string
  },
  scope?: TraceScope,
): boolean {
  let p: Record<string, any>
  try {
    p = queryParams(step.tool, step.paramsJson ? JSON.parse(step.paramsJson) as CallParams : null)
  } catch {
    return false
  }
  const arr = (v: unknown) => (Array.isArray(v) ? v.map(String) : v == null ? [] : [String(v)])
  const filterConditions = Array.isArray(p.conditions)
    ? p.conditions.filter((condition) => condition && typeof condition === 'object') as Record<string, unknown>[]
    : []
  const metrics = [...arr(p.metric), ...arr(p.metrics), ...filterConditions.flatMap((condition) => arr(condition.metric))]
  const products = [...arr(p.product_id), ...arr(p.product_ids)]
  const queryScope = p.scope && typeof p.scope === 'object' && !Array.isArray(p.scope)
    ? p.scope as Record<string, unknown>
    : null
  if (queryScope?.kind === 'product_ids') products.push(...arr(queryScope.product_ids))
  if (scope && queryScope?.kind === 'benchmark_all') products.push(...scope.products.map((product) => product.product_id))
  if (scope && queryScope?.kind === 'category') {
    const categories = new Set(arr(queryScope.categories))
    products.push(...scope.products.filter((product) => categories.has(product.category)).map((product) => product.product_id))
  }
  const periods = [...arr(p.period_id), ...arr(p.base_period_id), ...arr(p.target_period_id)]
  const requestedTime = selectedPeriods(p, scope ?? EMPTY_SCOPE)
  if (!metrics.length && !products.length && !periods.length && !requestedTime.specified) return false
  const evFam = ev.ns === 'pev' ? 'evidence' : OP_FAMILY[String(ev.op ?? '')] ?? ''
  const stepFam = stepFamily(step, p)
  const sameFam = Boolean(evFam) && evFam === stepFam
  if (evFam && stepFam && !sameFam) return false
  if (ev.metric && !metrics.includes(ev.metric)) return false
  // 无指标的行（如整行对照、取证登记）只有在同族时才算匹配到带指标的调用
  if (!ev.metric && metrics.length && !sameFam) return false
  if (ev.productId && products.length && !products.includes(ev.productId)) return false
  const evPeriods = ev.periods?.length
    ? ev.periods
    : ev.periodId ? [{ periodId: ev.periodId }] : []
  if (requestedTime.specified) {
    if (!requestedTime.refs.length || !evPeriods.length) return false
    if (requestedTime.slots) {
      if (requestedTime.slots.length !== evPeriods.length
        || requestedTime.slots.some((slot, index) => !slot.length
          || !slot.some((requested) => samePeriod(requested, evPeriods[index])))) return false
    } else if (!requestedTime.refs.some((requested) => evPeriods.some((actual) => samePeriod(requested, actual)))) {
      return false
    }
  } else if (ev.periodId && periods.length && !periods.includes(ev.periodId)) {
    return false
  }
  return true
}
