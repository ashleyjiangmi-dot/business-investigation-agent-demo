/**
 * 浏览器交付边界的事件/键面守卫：Agents SDK serving 负责产生当前 SSE 合同，
 * route 层再逐帧丢弃未知事件、剥除未知键，并对结构化结果做形状核验。
 * 本模块按当前事件合同过滤可见字段。
 */

export type WireEvent = { event: string; data: Record<string, unknown> }

export type CallParams = { op?: string; [key: string]: unknown } | null

/** 六类白名单事件 → 每类允许的键面。 */
const EVENT_KEYS: Record<string, readonly string[]> = {
  run_started: ['run_id', 'ts'],
  step_started: ['step_id', 'kind', 'label', 'params_digest'],
  step_finished: ['step_id', 'status', 'elapsed_ms', 'result_code'],
  answer_delta: ['text'],

  // 终态分别投影业务正文状态、实际查询范围和知识来源；可选信息缺失时由消费方处理。
  run_finished: ['status', 'answer_md', 'citations', 'result_status', 'has_business_answer',
    'query_scope', 'hidden_in_answer', 'report', 'knowledge_sources',
    'evidence_details', 'display_blocks'],
  error: ['reason_class'],
}

/**  结果合同 v1 的 schema 字面值（服务端 `core/wire_contract.REPORT_SCHEMA` 同值；TS 侧只防御性核对）。 */
const REPORT_SCHEMA = 'oagent_report_v1'

/**  展示块合同的 schema 字面值（服务端 `core/wire_contract.DISPLAY_BLOCKS_SCHEMA` 同值）。 */
const DISPLAY_BLOCKS_SCHEMA = 'oagent_display_blocks_v1'

/**
 *  证据详情项键面（服务端 `core/wire_contract._project_detail` 固定键集合，
 * 与 `agents_oagent/tests/test_uix04b_evidence_details.py` 的键面闭合断言同源；
 * 任一侧新增键必须双处同步）。`filterEvidenceDetail` 按下列字段重建对象=隐式白名单。
 */

export type EvidenceMetricRow = {
  metric: string
  metric_name: string | null
  unit: string | null
  value: string | number | null
  state: string | null
}

/**  日期窗口连续日读数行（`day_series:` 引用专用；date/value/state 逐字来自 Data 响应）。 */
export type EvidenceSeriesPoint = {
  date: string | null
  value: string | number | null
  state: string | null
  reason: string
}

export type EvidenceMembership = {
  rank?: unknown
  value?: unknown
  group_member?: boolean
  /** 集合中位数比较的已确认分组；缺失=旧 SSE 或无法确定，不猜。 */
  group?: 'high' | 'equal' | 'low'
}

export type EvidenceDetail = {
  handle: string
  ref: string
  locate: 'resolved' | 'missing'
  source: string
  op: string
  query_id: string
  metric: string | null
  metric_name: string | null
  unit: string | null
  product_id: string | null
  product_name: string | null
  period: EvidencePeriod | null
  base: EvidencePeriod | null
  target: EvidencePeriod | null
  value: string | number | null
  state: string | null
  reason: string
  change: { absolute: string | number | null; percent: string | number | null; direction: string | null } | null
  denominator: { kind: string | null; value: string | number | null; note: string | null } | null
  collection: EvidenceCollection | null
  membership: EvidenceMembership | null
  metric_rows: EvidenceMetricRow[] | null
  states: string[] | null
  /** 连续日读数（`day_series:` 引用专用；其他引用为 null） */
  series: EvidenceSeriesPoint[] | null
  note: string
}

export type EvidencePeriod = {
  period_id: string
  label: string | null
  start_date: string | null
  end_date: string | null
  length_days: number | null
  value: string | number | null
  unit: string | null
  state: string | null
  /** states 行（object_state 引用）专用：该侧窗口状态与跨期变化事实（diff/value 行无此字段） */
  window_state?: string | null
  changes?: { field: string | null; value: string | number | null }[] | null
}

export type EvidenceRankingMember = {
  rank: number | null
  product_id: string | null
  value: string | number | null
}

export type EvidenceMissingMember = {
  product_id: string | null
  value: string | number | null
  state: string | null
  reason: string | null
}

/** 集合引用的既有业务键保持原样；rank 引用新增的 Data 成员字段做类型化消费。 */
export type EvidenceCollection = Record<string, unknown> & {
  kind?: string
  aggregate?: string | null
  sum?: WireScalar
  complete?: boolean | null
  sum_scope?: string | null
  selected_n?: number | null
  comparable_n?: number | null
  non_comparable_n?: number | null
  non_comparable_by_state?: Record<string, number>
  product_ids?: string[]
  objects?: string[]
  ranking?: EvidenceRankingMember[]
  missing_group?: EvidenceMissingMember[]
}

const isStr = (x: unknown): x is string => typeof x === 'string'
const isNumOrNull = (x: unknown): x is number | null => x === null || typeof x === 'number'
const isStrNumOrNull = (x: unknown): x is string | number | null => x === null || typeof x === 'string' || typeof x === 'number'

function filterEvidencePeriod(v: unknown): EvidencePeriod | null {
  if (v == null) return null
  if (typeof v !== 'object' || Array.isArray(v)) return null
  const o = v as Record<string, unknown>
  if (!isStr(o.period_id)) return null
  return {
    period_id: o.period_id,
    label: typeof o.label === 'string' ? o.label : null,
    start_date: typeof o.start_date === 'string' ? o.start_date : null,
    end_date: typeof o.end_date === 'string' ? o.end_date : null,
    length_days: isNumOrNull(o.length_days) ? o.length_days : null,
    value: isStrNumOrNull(o.value) ? o.value : null,
    unit: typeof o.unit === 'string' ? o.unit : null,
    state: typeof o.state === 'string' ? o.state : null,
    window_state: typeof o.window_state === 'string' ? o.window_state : null,
    changes: Array.isArray(o.changes)
      ? (o.changes as unknown[])
          .filter((c): c is Record<string, unknown> => Boolean(c) && typeof c === 'object')
          .map((c) => ({
            field: typeof c.field === 'string' ? c.field : null,
            value: isStrNumOrNull(c.value) ? c.value : null,
          }))
      : null,
  }
}

function filterEvidenceCollection(v: unknown): EvidenceCollection | null {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null
  const c = v as Record<string, unknown>
  const out = { ...c } as EvidenceCollection
  if (Array.isArray(c.objects)) out.objects = c.objects.filter(isStr)
  if (Array.isArray(c.ranking)) {
    out.ranking = c.ranking
      .filter((x): x is Record<string, unknown> => Boolean(x) && typeof x === 'object' && !Array.isArray(x))
      .map((x) => ({
        rank: typeof x.rank === 'number' ? x.rank : null,
        product_id: isStr(x.product_id) ? x.product_id : null,
        value: isStrNumOrNull(x.value) ? x.value : null,
      }))
  }
  if (Array.isArray(c.missing_group)) {
    out.missing_group = c.missing_group
      .filter((x): x is Record<string, unknown> => Boolean(x) && typeof x === 'object' && !Array.isArray(x))
      .map((x) => ({
        product_id: isStr(x.product_id) ? x.product_id : null,
        value: isStrNumOrNull(x.value) ? x.value : null,
        state: isStr(x.state) ? x.state : null,
        reason: isStr(x.reason) ? x.reason : null,
      }))
  }
  if (c.kind === 'set_sum') {
    out.kind = 'set_sum'
    out.aggregate = isStr(c.aggregate) ? c.aggregate : null
    out.sum = isStrNumOrNull(c.sum) ? c.sum : null
    out.complete = typeof c.complete === 'boolean' ? c.complete : null
    out.sum_scope = isStr(c.sum_scope) ? c.sum_scope : null
    out.selected_n = typeof c.selected_n === 'number' ? c.selected_n : null
    out.comparable_n = typeof c.comparable_n === 'number' ? c.comparable_n : null
    out.non_comparable_n = typeof c.non_comparable_n === 'number' ? c.non_comparable_n : null
    out.non_comparable_by_state = asStateCounts(c.non_comparable_by_state)
    out.product_ids = Array.isArray(c.product_ids) ? c.product_ids.filter(isStr) : []
  }
  return out
}

function filterEvidenceMembership(v: unknown): EvidenceMembership | null {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null
  const m = v as Record<string, unknown>
  const out: EvidenceMembership = {}
  if ('rank' in m) out.rank = m.rank
  if ('value' in m) out.value = m.value
  if (m.group_member === true) out.group_member = true
  if (m.group === 'high' || m.group === 'equal' || m.group === 'low') out.group = m.group
  return out
}

/**
 * 单条证据详情项守卫：键白名单 + 关键类型核对；畸形项**整项丢弃**（页面退「暂不可核对」，
 * 不带病渲染，也不让畸形载荷进 DOM）。locate 只认服务端两种取值。
 */
function filterEvidenceDetail(v: unknown): EvidenceDetail | null {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null
  const o = v as Record<string, unknown>
  if (!isStr(o.handle) || !isStr(o.ref) || (o.locate !== 'resolved' && o.locate !== 'missing')) return null
  if (!isStr(o.source) || !isStr(o.op) || !isStr(o.query_id)) return null
  if (!isStr(o.reason) || !isStr(o.note)) return null
  const strOrNull = (x: unknown) => (typeof x === 'string' ? x : null)
  const out: EvidenceDetail = {
    handle: o.handle, ref: o.ref, locate: o.locate,
    source: o.source, op: o.op, query_id: o.query_id,
    metric: strOrNull(o.metric), metric_name: strOrNull(o.metric_name), unit: strOrNull(o.unit),
    product_id: strOrNull(o.product_id), product_name: strOrNull(o.product_name),
    period: filterEvidencePeriod(o.period),
    base: filterEvidencePeriod(o.base),
    target: filterEvidencePeriod(o.target),
    value: isStrNumOrNull(o.value) ? o.value : null,
    state: strOrNull(o.state),
    reason: o.reason,
    change: o.change && typeof o.change === 'object' && !Array.isArray(o.change)
      ? {
          absolute: isStrNumOrNull((o.change as Record<string, unknown>).absolute) ? (o.change as Record<string, unknown>).absolute as string | number : null,
          percent: isStrNumOrNull((o.change as Record<string, unknown>).percent) ? (o.change as Record<string, unknown>).percent as string | number : null,
          direction: strOrNull((o.change as Record<string, unknown>).direction),
        }
      : null,
    denominator: o.denominator && typeof o.denominator === 'object' && !Array.isArray(o.denominator)
      ? (o.denominator as Record<string, unknown>) as EvidenceDetail['denominator']
      : null,
    collection: filterEvidenceCollection(o.collection),
    membership: filterEvidenceMembership(o.membership),
    metric_rows: Array.isArray(o.metric_rows)
      ? (o.metric_rows as unknown[])
          .filter((r): r is Record<string, unknown> =>
            Boolean(r) && typeof r === 'object' && isStr((r as Record<string, unknown>).metric))
          .map((r) => ({
            metric: r.metric as string,
            metric_name: typeof r.metric_name === 'string' ? r.metric_name : null,
            unit: typeof r.unit === 'string' ? r.unit : null,
            value: isStrNumOrNull(r.value) ? r.value : null,
            state: typeof r.state === 'string' ? r.state : null,
          }))
      : null,
    states: Array.isArray(o.states) ? (o.states as unknown[]).map(String) : null,
    series: Array.isArray(o.series)
      ? (o.series as unknown[])
          .filter((s): s is Record<string, unknown> => Boolean(s) && typeof s === 'object')
          .map((s) => ({
            date: typeof s.date === 'string' ? s.date : null,
            value: isStrNumOrNull(s.value) ? s.value : null,
            state: typeof s.state === 'string' ? s.state : null,
            reason: typeof s.reason === 'string' ? s.reason : '',
          }))
      : null,
    note: o.note,
  }
  return out
}

/** 白名单键剥除后逐项形状守卫；非数组=整键置 null（缺失/null=旧 SSE 的正常兼容路径）。 */
function filterEvidenceDetails(v: unknown): EvidenceDetail[] | null {
  if (!Array.isArray(v)) return null
  const kept = v.map(filterEvidenceDetail).filter((d): d is EvidenceDetail => d !== null)
  const dropped = v.length - kept.length
  if (dropped > 0) console.warn(`[evidence_details] 中 ${dropped} 项形状不符，已丢弃（该依据按暂不可核对处理）`)
  return kept
}

export type ReportV1 = {
  schema: typeof REPORT_SCHEMA
  summary: string
  /**  可选短结论：服务端已按结论节做逐字子串校验（不匹配=缺键）；此处只做类型过滤 */
  answer_line?: string
  key_findings: string[]
  next_steps: string[]
  caveats: string[]
  detail_md: string
}

/**
 * report 深层守卫：白名单键外剥除；形状不合合同（非 string/string[] 或 schema 换代）整键置 null。
 * null/缺失=正常降级路径，消费方回退完整 `answer_md`；畸形结构不进 DOM。
 */
function filterReport(v: unknown): ReportV1 | null {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null
  const r = v as Record<string, unknown>
  if (r.schema !== REPORT_SCHEMA) return null
  const isStr = (x: unknown): x is string => typeof x === 'string'
  const isStrArr = (x: unknown): x is string[] =>
    Array.isArray(x) && x.every((item) => typeof item === 'string')
  if (!isStr(r.summary) || !isStr(r.detail_md) ||
      !isStrArr(r.key_findings) || !isStrArr(r.next_steps) || !isStrArr(r.caveats)) return null
  const out: ReportV1 = {
    schema: REPORT_SCHEMA,
    summary: r.summary,
    key_findings: r.key_findings,
    next_steps: r.next_steps,
    caveats: r.caveats,
    detail_md: r.detail_md,
  }
  // 可选短结论透传（非串/空白=不带该键，旧 SSE 与校验失败路径的正常缺省形态）
  if (isStr(r.answer_line) && r.answer_line.trim()) out.answer_line = r.answer_line
  return out
}

/**
 * 逐层校验展示块的键与值类型，按服务端字段白名单重建投影。
 * 无可用投影时交由正文路径呈现；前端不复算数值或解释模型自由声明。
 * display.metrics 可为标量或数组，标量归一为单元素数组。
 */

/** Wire 标量：合同值域 string/number/null（布尔与对象不进标量位）。 */
export type WireScalar = string | number | null

export type DisplayFactSide = { period_id: string | null; value: WireScalar }

/**  日期窗口事实的真实起止日期身份（`window_object`/`window_compare`/`day_series` 携带）。 */
export type DisplayFactWindow = {
  start_date: string | null
  end_date: string | null
  length_days: number | null
}

/**  日期窗口逐日读数（`day_series` 事实携带；date/value/state 逐字来自 Data 响应）。 */
export type DisplaySeriesPoint = {
  date: string | null
  value: WireScalar
  state: string | null
}

export type DisplayFact = {
  handle: string
  ref: string
  kind: string | null
  metric: string | null
  product_id: string | null
  period_id: string | null
  value: WireScalar
  unit: string | null
  state: string | null
  base: DisplayFactSide | null
  target: DisplayFactSide | null
  absolute: WireScalar
  percent: WireScalar
  direction: string | null
  /** 日期窗口身份（窗口事实专用；旧周期事实为 null） */
  window: DisplayFactWindow | null
  /** 逐日读数序列（`day_series` 事实专用；其他事实为 null） */
  day_series: DisplaySeriesPoint[] | null
  /** 单期集合合计事实（`kind='set_sum'` 专用）；单点投影、键面闭合 */
  single_period_totals: DisplaySinglePeriodTotals | null
}

export type DisplayDisplay = {
  subjects: string[]
  metrics: string[]
  periods: string[]
}

export type DisplayMedianMember = { handle: string | null; product_id: string | null; value: WireScalar }

export type DisplayMedianGroup = {
  metric: string | null
  period_id: string | null
  n: number | null
  median: WireScalar
  basis: string | null
  members: DisplayMedianMember[]
  excluded: { handle: string | null; product_id: string | null; state: string | null }[]
}

export type DisplayDirections = {
  up: number | null
  down: number | null
  flat: number | null
  n: number | null
  basis: string | null
  other: { handle: string | null; state: string | null; note: string | null }[]
}

export type DisplayRank = {
  product_id: string | null
  handle: string | null
  metric: string | null
  period_id: string | null
  value: WireScalar
  set_n: number | null
  rank_desc: number | null
  rank_asc: number | null
  basis: string | null
}

export type DisplayToolMedian = {
  metric: string | null
  period_id: string | null
  product_ids: string[]
  unit: string | null
  handle: string | null
  ref: string | null
  median: WireScalar
  label: string | null
}

export type DisplayRankingMissing = {
  product_id: string | null
  value: WireScalar
  state: string | null
  reason: string | null
}

/**
 * 集合块内的**工具权威整体排名**（服务端 `recomputed.rankings` 键面镜像）。
 * `rows` 的名次/顺序/原值逐字来自 Data 的 `ranking`（不是对 `input_handles` 的局部复算
 * `ranks`）；`selected_n`/`usable_n`/`missing_n` 与 `period_id`（目录周期 ID 或 `start~end`
 * 日期窗口身份）如实标注「本次选中集合」与真实时间。本层只做类型过滤与确定性格式化，
 * 不重排名、不按值复算名次、不补造缺失成员。
 */
export type DisplayRankingRow = {
  rank: number | null
  product_id: string | null
  value: WireScalar
}

export type DisplayRanking = {
  kind: string | null
  kind_name: string | null
  metric: string | null
  unit: string | null
  period_id: string | null
  window: DisplayFactWindow | null
  selected_n: number | null
  usable_n: number | null
  missing_n: number | null
  product_ids: string[]
  missing_group: DisplayRankingMissing[]
  handle: string | null
  ref: string | null
  rows: DisplayRankingRow[]
}

/**
 *  单期集合合计（`kind='set_sum'` 引用事实携带）：`value aggregate='sum'` 时 Data 按真实
 * 行对请求集合 × 指标 × 单期时间求和；有效 0 参与合计、缺失不代 0；全请求成员均有效才称
 * 「本次集合完整合计」，有缺失标「已知可用值合计（不含缺失）」，全不可用为 unknown。
 * 不按品类拆分。键面闭合，数值/成员/覆盖/状态/时间身份均原样取自
 * 服务端 Data 条目，不做重算。
 */
export type DisplaySinglePeriodTotals = {
  metric: string | null
  metric_name: string | null
  unit: string | null
  aggregate: string | null
  sum: WireScalar
  complete: boolean | null
  sum_scope: string | null
  selected_n: number | null
  comparable_n: number | null
  non_comparable_n: number | null
  non_comparable_by_state: Record<string, number>
  product_ids: string[]
  period_id: string | null
  start_date: string | null
  end_date: string | null
  window_identity: string | null
  note: string | null
}

/**
 *  comparison 块的服务端复算分组合计（服务端 `recomputed.group_totals` 键面镜像）：
 * 合计**按目录可信 `category` 拆分成（品类 × 指标 × 两期）**——混装多类目时多条各自归属，
 * 不产跨品类混合合计；`category` 只来自服务端商品目录，模型叙述文字不是此字段来源。
 * 合计只含块声明集合内**两期均可比**的读数，不可比读数按状态分类披露、不代 0；
 * `percent_change_state !== 'ok'` 时 `percent_change=null`（基期合计 0 或无可比商品），
 * 呈现方不得按 0% 处理。 只同步类型与透传（零渲染/零语义判断），首屏呈现归 。
 */
export type DisplayGroupTotal = {
  category: string | null
  metric: string | null
  unit: string | null
  base_period_id: string | null
  target_period_id: string | null
  subjects_n: number | null
  comparable_n: number | null
  non_comparable_n: number | null
  non_comparable_by_state: Record<string, number>
  base_sum: WireScalar
  target_sum: WireScalar
  absolute_change: WireScalar
  percent_change: WireScalar
  percent_change_state: string | null
  sum_scope: string | null
  basis: string | null
}

/**  cohort_list 权威集合段：全部值来自服务端已绑定的 Data 权威结果（非模型声明）。 */
export type DisplayCohortCondition = { metric: string | null; field: string | null; operator: string | null; value: WireScalar }
export type DisplayCohortReading = { metric: string | null; field: string | null; value: WireScalar }
export type DisplayCohortRow = { product_id: string | null; readings: DisplayCohortReading[] }
export type DisplayCohortUnknown = { product_id: string | null; reasons: string[] }
export type DisplayCohortDeclared = { matches: string[] | null; match_count: number | null; scope_count: number | null }
export type DisplayCohortReconciliation = {
  status: string | null
  missing_ids: string[]
  extra_ids: string[]
  duplicate_ids: string[]
  match_count_declared: number | null
  match_count_actual: number | null
  scope_count_declared: number | null
  scope_count_actual: number | null
}
export type DisplayCohort = {
  ref: string | null
  handle: string | null
  query_id: string | null
  /** authority=直接采用权威集合；corrected=模型声明与权威不一致、已按权威填充（不冒充模型名单已核验） */
  binding: string | null
  time_mode: string | null
  period_id: string | null
  base_period_id: string | null
  target_period_id: string | null
  scope_kind: string | null
  scope_count: number | null
  /** 范围身份指纹（Data 已核实的完整 scope 成员集，含未命中成员；顺序无关）。
   *  展示层判「同一权威集合」用；旧 SSE 缺键＝null（消费侧按信息不足保守处理）。 */
  scope_signature: string | null
  conditions: DisplayCohortCondition[]
  ordered_by: string | null
  match_count: number | null
  nonmatch_count: number | null
  predicate_unknown_count: number | null
  confirmed_share_percent: WireScalar
  share_denominator: string | null
  members_complete: boolean | null
  match_rows: DisplayCohortRow[]
  unknown_members: DisplayCohortUnknown[]
  declared: DisplayCohortDeclared | null
  reconciliation: DisplayCohortReconciliation | null
}

export type DisplayRecomputed = {
  median_by_group: DisplayMedianGroup[]
  /** 缺席/空对象=null（块未声明方向计数时不出该段） */
  direction_counts: DisplayDirections | null
  ranks: DisplayRank[]
  tool_returned: DisplayToolMedian[]
  /** comparison 块的服务端复算分组合计（键位恒在；无输入/口径不完整时=[]） */
  group_totals: DisplayGroupTotal[]
  /** 集合块的工具权威整体排名（键位恒在；无排名引用/中位数引用=[]，不伪造榜单） */
  rankings: DisplayRanking[]
}

export type DisplayVerification = {
  refs: { total: number | null; verified_n: number | null; unresolved: string[] }
}

export type DisplayBlock = {
  index: number
  type: string
  display: DisplayDisplay
  verified_facts: DisplayFact[]
  recomputed: DisplayRecomputed
  verification: DisplayVerification
  /** 真实引用/口径/计算问题的块级局部说明（服务端确定性路径产生；不含自由文字扫描结果） */
  data_issues: string[]
  /** cohort_list 权威集合段（非 cohort 块/旧 SSE=null；未绑定时不携带集合数据） */
  cohort: DisplayCohort | null
}

export type DisplayBlocks = {
  schema: typeof DISPLAY_BLOCKS_SCHEMA
  run_id: string
  parse_status: string
  blocks: DisplayBlock[]
}

const DISPLAY_TOP_KEYS = ['schema', 'run_id', 'parse_status', 'blocks'] as const
const DISPLAY_BLOCK_KEYS = ['index', 'type', 'display', 'verified_facts',
  'recomputed', 'cohort', 'verification', 'data_issues'] as const
const DISPLAY_FACT_KEYS = ['handle', 'ref', 'kind', 'metric', 'product_id', 'period_id',
  'value', 'unit', 'state', 'base', 'target', 'absolute', 'percent', 'direction',
  //  日期窗口：与服务端 `core.wire_contract._DISPLAY_FACT_KEYS` 同源（任一侧换代必须双处同步）。
  'window', 'day_series'] as const
const DISPLAY_DISPLAY_KEYS = ['subjects', 'metrics', 'periods'] as const
const DISPLAY_RECOMPUTED_KEYS = ['median_by_group', 'direction_counts', 'ranks', 'tool_returned',
  'group_totals', 'rankings'] as const
const DISPLAY_GROUP_KEYS = ['metric', 'period_id', 'n', 'median', 'basis', 'members', 'excluded'] as const
const DISPLAY_MEMBER_KEYS = ['handle', 'product_id', 'value'] as const
const DISPLAY_EXCLUDED_KEYS = ['handle', 'product_id', 'state'] as const
const DISPLAY_DIRS_KEYS = ['up', 'down', 'flat', 'n', 'basis', 'other'] as const
const DISPLAY_OTHER_KEYS = ['handle', 'state', 'note'] as const
const DISPLAY_RANK_KEYS = ['product_id', 'handle', 'metric', 'period_id', 'value', 'set_n',
  'rank_desc', 'rank_asc', 'basis'] as const
// 工具返回读数补传源 fact 真实口径与引用关联（减法：撤掉块状态后不依赖模型 display 声明背书）。
const DISPLAY_TOOLMEDIAN_KEYS = ['metric', 'period_id', 'product_ids', 'unit', 'handle', 'ref', 'median', 'label'] as const
// 与服务端 `core.wire_contract._DISPLAY_GROUP_TOTAL_KEYS` 同源（任一侧换代必须双处同步）。
const DISPLAY_GROUP_TOTAL_KEYS = ['category', 'metric', 'unit', 'base_period_id',
  'target_period_id', 'subjects_n', 'comparable_n', 'non_comparable_n',
  'non_comparable_by_state', 'base_sum', 'target_sum', 'absolute_change', 'percent_change',
  'percent_change_state', 'sum_scope', 'basis'] as const
// 与服务端 `core/wire_contract._DISPLAY_RANKING_*` 同源（任一侧换代必须双处同步）。
const DISPLAY_RANKING_KEYS = ['kind', 'kind_name', 'metric', 'unit', 'period_id', 'window',
  'selected_n', 'usable_n', 'missing_n', 'product_ids', 'missing_group', 'handle', 'ref', 'rows'] as const
const DISPLAY_RANKING_ROW_KEYS = ['rank', 'product_id', 'value'] as const
const DISPLAY_RANKING_MISSING_KEYS = ['product_id', 'value', 'state', 'reason'] as const
const DISPLAY_REFS_KEYS = ['total', 'verified_n', 'unresolved'] as const
// cohort_list 权威集合段键面（与服务端 `core/wire_contract._COHORT_*` 常量同源；
// 任一侧换代必须双处同步）。未绑定块服务端已置 null，此处只防御性重建形状。
const DISPLAY_COHORT_KEYS = ['ref', 'handle', 'query_id', 'binding', 'time_mode', 'period_id',
  'base_period_id', 'target_period_id', 'scope_kind', 'scope_count', 'scope_signature', 'conditions', 'ordered_by',
  'match_count', 'nonmatch_count', 'predicate_unknown_count', 'confirmed_share_percent',
  'share_denominator', 'members_complete', 'match_rows', 'unknown_members', 'declared',
  'reconciliation'] as const
const DISPLAY_COHORT_COND_KEYS = ['metric', 'field', 'operator', 'value'] as const
const DISPLAY_COHORT_ROW_KEYS = ['product_id', 'readings'] as const
const DISPLAY_COHORT_READING_KEYS = ['metric', 'field', 'value'] as const
const DISPLAY_COHORT_UNKNOWN_KEYS = ['product_id', 'reasons'] as const
const DISPLAY_COHORT_DECLARED_KEYS = ['matches', 'match_count', 'scope_count'] as const
const DISPLAY_COHORT_RECON_KEYS = ['status', 'missing_ids', 'extra_ids', 'duplicate_ids',
  'match_count_declared', 'match_count_actual', 'scope_count_declared', 'scope_count_actual'] as const

const isObj = (x: unknown): x is Record<string, unknown> =>
  Boolean(x) && typeof x === 'object' && !Array.isArray(x)

/** 固定键位重建（缺键补 null、白名单外键丢弃）——与服务端 `_pick` 同一手法。 */
function pickWire(v: unknown, keys: readonly string[]): Record<string, unknown> {
  const o = isObj(v) ? v : {}
  const out: Record<string, unknown> = {}
  for (const k of keys) out[k] = o[k] ?? null
  return out
}

/** 集合比较使用的实际请求成员，不逐个计入调查对象列表。 */
export type BaselineSet = {
  op: string | null
  metric: string | null
  period_ids: string[]
  product_ids: string[]
  product_n: number | null
}

/** 筛选范围的 product_n 取服务端权威分母；无回显时保持 null，不推定为零。 */
export type FilterScope = {
  op: string | null
  scope_kind: string | null
  product_ids: string[]
  product_n: number | null
}

/** 本次成功查询的实际调查对象、比较基准与筛选范围。 */
export type QueryScope = {
  queries_n: number
  investigation_product_ids: string[]
  investigation_product_n: number
  per_metric: Record<string, string[]>
  baseline_sets: BaselineSet[]
  filter_scopes: FilterScope[]
}

/**
 * 校验实际查询范围投影，并按字段形状重建指标成员、比较集合和筛选范围。
 * 查询数或成员字段非法时返回 null，不从结果数值推测范围。
 */
function filterQueryScope(v: unknown): QueryScope | null {
  if (!isObj(v)) return null
  const o = v as Record<string, unknown>
  const n = asNumOrNull(o.queries_n)
  if (n === null || n <= 0) return null
  const perMetric: Record<string, string[]> = {}
  if (isObj(o.per_metric)) {
    for (const [k, val] of Object.entries(o.per_metric as Record<string, unknown>)) {
      perMetric[k] = asStrArr(val)
    }
  }
  const baselineSets = Array.isArray(o.baseline_sets)
    ? o.baseline_sets.filter(isObj).map((b) => {
        const bo = b as Record<string, unknown>
        return { op: asStrOrNull(bo.op), metric: asStrOrNull(bo.metric),
                 period_ids: asStrArr(bo.period_ids), product_ids: asStrArr(bo.product_ids),
                 product_n: asNumOrNull(bo.product_n) }
      })
    : []
  const filterScopes = Array.isArray(o.filter_scopes)
    ? o.filter_scopes.filter(isObj).map((f) => {
        const fo = f as Record<string, unknown>
        return { op: asStrOrNull(fo.op), scope_kind: asStrOrNull(fo.scope_kind),
                 product_ids: asStrArr(fo.product_ids), product_n: asNumOrNull(fo.product_n) }
      })
    : []
  const ids = asStrArr(o.investigation_product_ids)
  return {
    queries_n: n,
    investigation_product_ids: ids,
    investigation_product_n: asNumOrNull(o.investigation_product_n) ?? ids.length,
    per_metric: perMetric,
    baseline_sets: baselineSets,
    filter_scopes: filterScopes,
  }
}

/**
 * 知识来源：本 run 通过 `knowledge_search` **实际返回**的文档（`doc_id/title/doc_type`）。
 * 与 `[E]` 数值证据无关：不进 Evidence Guard、不参与核验，只服务正文知识引用的展示与来源卡片。
 */
export type KnowledgeSource = {
  doc_id: string
  title: string
  doc_type: 'guide' | 'case' | ''
}

/**
 * 知识来源列表的形状核验（键面单点＝服务端 `core/wire_contract.KNOWLEDGE_SOURCE_KEYS` /
 * `knowledge_sources_wire`）：逐项要求非空 `doc_id`，`doc_type` 只认 guide/case（其余留空，不猜）；
 * 畸形项丢弃、整键不可用=null（缺键＝旧 SSE 兼容：正文知识引用退纯文本，不伪造来源）。
 */
function filterKnowledgeSources(v: unknown): KnowledgeSource[] | null {
  if (!Array.isArray(v)) return null
  const out: KnowledgeSource[] = []
  const seen = new Set<string>()
  for (const item of v) {
    if (!isObj(item)) continue
    const o = item as Record<string, unknown>
    const docId = asStrOrNull(o.doc_id)
    if (!docId || seen.has(docId)) continue
    seen.add(docId)
    const kind = asStrOrNull(o.doc_type)
    out.push({
      doc_id: docId,
      title: asStrOrNull(o.title) ?? '',
      doc_type: kind === 'guide' || kind === 'case' ? kind : '',
    })
  }
  return out.length ? out : null
}

function pickWireList(v: unknown, keys: readonly string[]): Record<string, unknown>[] {
  return Array.isArray(v) ? v.filter(isObj).map((x) => pickWire(x, keys)) : []
}

// ---- 值级类型核对：畸形标量降为 null/整条丢弃，不让带病值进视图模型 ----

const asStrOrNull = (x: unknown): string | null => (isStr(x) ? x : null)
const asNumOrNull = (x: unknown): number | null =>
  typeof x === 'number' && Number.isFinite(x) ? x : null
const asBoolOrNull = (x: unknown): boolean | null => (typeof x === 'boolean' ? x : null)
/** Wire 标量位：string/number 透传，其余（布尔/对象/数组/undefined/NaN）归 null。 */
const asScalar = (x: unknown): WireScalar => {
  if (isStr(x)) return x
  if (typeof x === 'number' && Number.isFinite(x)) return x
  return null
}
/** string|string[] 归一：标量包成单元素数组（metrics="M1"），数组只留串项。 */
const asStrArr = (x: unknown): string[] =>
  isStr(x) ? [x] : Array.isArray(x) ? x.filter(isStr) : []

/** 状态→计数映射（未知状态码透传为键；非计数/畸形项丢弃，不猜值、不补 0）。 */
const asStateCounts = (x: unknown): Record<string, number> => {
  if (!isObj(x)) return {}
  const out: Record<string, number> = {}
  for (const key of Object.keys(x)) {
    const v = x[key]
    if (typeof v === 'number' && Number.isFinite(v)) out[key] = v
  }
  return out
}

/** 校验事实与排名条目的日期窗口身份。 */
function asFactWindow(v: unknown): DisplayFactWindow | null {
  if (!isObj(v)) return null
  return {
    start_date: asStrOrNull(v.start_date),
    end_date: asStrOrNull(v.end_date),
    length_days: asNumOrNull(v.length_days),
  }
}

/** 单期集合合计事实投影（`single_period_totals` 键面镜像，零复算）。 */
function filterSinglePeriodTotals(v: unknown): DisplaySinglePeriodTotals | null {
  if (!isObj(v)) return null
  return {
    metric: asStrOrNull(v.metric),
    metric_name: asStrOrNull(v.metric_name),
    unit: asStrOrNull(v.unit),
    aggregate: asStrOrNull(v.aggregate),
    sum: asScalar(v.sum),
    complete: (typeof v.complete === 'boolean' ? v.complete : null),
    sum_scope: asStrOrNull(v.sum_scope),
    selected_n: (typeof v.selected_n === 'number' ? v.selected_n : null),
    comparable_n: (typeof v.comparable_n === 'number' ? v.comparable_n : null),
    non_comparable_n: (typeof v.non_comparable_n === 'number' ? v.non_comparable_n : null),
    non_comparable_by_state: asStateCounts(v.non_comparable_by_state),
    product_ids: Array.isArray(v.product_ids) ? (v.product_ids as unknown[]).filter(isStr) : [],
    period_id: asStrOrNull(v.period_id),
    start_date: asStrOrNull(v.start_date),
    end_date: asStrOrNull(v.end_date),
    window_identity: asStrOrNull(v.window_identity),
    note: asStrOrNull(v.note),
  }
}

function filterDisplayFact(v: unknown): DisplayFact | null {
  if (!isObj(v) || !isStr(v.handle) || !isStr(v.ref)) return null
  const side = (s: unknown): DisplayFactSide | null => {
    if (!isObj(s)) return null
    return { period_id: asStrOrNull(s.period_id), value: asScalar(s.value) }
  }
  const win = asFactWindow(v.window)
  return {
    handle: v.handle,
    ref: v.ref,
    kind: asStrOrNull(v.kind),
    metric: asStrOrNull(v.metric),
    product_id: asStrOrNull(v.product_id),
    period_id: asStrOrNull(v.period_id),
    value: asScalar(v.value),
    unit: asStrOrNull(v.unit),
    state: asStrOrNull(v.state),
    base: side(v.base),
    target: side(v.target),
    absolute: asScalar(v.absolute),
    percent: asScalar(v.percent),
    direction: asStrOrNull(v.direction),
    window: win,
    day_series: Array.isArray(v.day_series)
      ? (v.day_series as unknown[])
          .filter(isObj)
          .map((d) => ({
            date: asStrOrNull((d as Record<string, unknown>).date),
            value: asScalar((d as Record<string, unknown>).value),
            state: asStrOrNull((d as Record<string, unknown>).state),
          }))
      : null,
    single_period_totals: filterSinglePeriodTotals(v.single_period_totals),
  }
}

/** cohort_list 权威集合段守卫（畸形段整段置 null=该块退通用布局，不带病渲染）。 */
function filterCohort(v: unknown): DisplayCohort | null {
  if (!isObj(v) || !isStr(v.ref) || !isStr(v.handle)) return null
  const conds = pickWireList(v.conditions, DISPLAY_COHORT_COND_KEYS).map((c) => ({
    metric: asStrOrNull(c.metric),
    field: asStrOrNull(c.field),
    operator: asStrOrNull(c.operator),
    value: asScalar(c.value),
  }))
  const rows = pickWireList(v.match_rows, DISPLAY_COHORT_ROW_KEYS).map((r) => ({
    product_id: asStrOrNull(r.product_id),
    readings: pickWireList(r.readings, DISPLAY_COHORT_READING_KEYS).map((rd) => ({
      metric: asStrOrNull(rd.metric),
      field: asStrOrNull(rd.field),
      value: asScalar(rd.value),
    })),
  }))
  const unknowns = pickWireList(v.unknown_members, DISPLAY_COHORT_UNKNOWN_KEYS).map((u) => ({
    product_id: asStrOrNull(u.product_id),
    reasons: asStrArr(u.reasons),
  }))
  const decRaw = isObj(v.declared) ? v.declared : null
  const declared: DisplayCohortDeclared | null = decRaw
    ? {
        matches: Array.isArray(decRaw.matches) ? decRaw.matches.filter(isStr) : null,
        match_count: asNumOrNull(decRaw.match_count),
        scope_count: asNumOrNull(decRaw.scope_count),
      }
    : null
  const recRaw = isObj(v.reconciliation) ? v.reconciliation : null
  const reconciliation: DisplayCohortReconciliation | null = recRaw
    ? {
        status: asStrOrNull(recRaw.status),
        missing_ids: asStrArr(recRaw.missing_ids),
        extra_ids: asStrArr(recRaw.extra_ids),
        duplicate_ids: asStrArr(recRaw.duplicate_ids),
        match_count_declared: asNumOrNull(recRaw.match_count_declared),
        match_count_actual: asNumOrNull(recRaw.match_count_actual),
        scope_count_declared: asNumOrNull(recRaw.scope_count_declared),
        scope_count_actual: asNumOrNull(recRaw.scope_count_actual),
      }
    : null
  const out = pickWire(v, DISPLAY_COHORT_KEYS) as unknown as DisplayCohort
  out.ref = asStrOrNull(v.ref)
  out.handle = asStrOrNull(v.handle)
  out.query_id = asStrOrNull(v.query_id)
  out.binding = asStrOrNull(v.binding)
  out.time_mode = asStrOrNull(v.time_mode)
  out.period_id = asStrOrNull(v.period_id)
  out.base_period_id = asStrOrNull(v.base_period_id)
  out.target_period_id = asStrOrNull(v.target_period_id)
  out.scope_kind = asStrOrNull(v.scope_kind)
  out.scope_count = asNumOrNull(v.scope_count)
  out.scope_signature = asStrOrNull(v.scope_signature)
  out.ordered_by = asStrOrNull(v.ordered_by)
  out.match_count = asNumOrNull(v.match_count)
  out.nonmatch_count = asNumOrNull(v.nonmatch_count)
  out.predicate_unknown_count = asNumOrNull(v.predicate_unknown_count)
  out.confirmed_share_percent = asScalar(v.confirmed_share_percent)
  out.share_denominator = asStrOrNull(v.share_denominator)
  out.members_complete = asBoolOrNull(v.members_complete)
  out.conditions = conds
  out.match_rows = rows
  out.unknown_members = unknowns
  out.declared = declared
  out.reconciliation = reconciliation
  return out
}

function filterDisplayBlock(v: unknown): DisplayBlock | null {
  if (!isObj(v)) return null
  if (typeof v.index !== 'number' || !isStr(v.type)) return null

  const d = isObj(v.display) ? v.display : {}
  const display: DisplayDisplay = {
    subjects: asStrArr(d.subjects),
    metrics: asStrArr(d.metrics),
    periods: asStrArr(d.periods),
  }

  const facts = Array.isArray(v.verified_facts)
    ? v.verified_facts.map(filterDisplayFact).filter((f): f is DisplayFact => f !== null)
    : []

  const rec = isObj(v.recomputed) ? v.recomputed : {}
  const groups: DisplayMedianGroup[] = Array.isArray(rec.median_by_group)
    ? rec.median_by_group.filter(isObj).map((g) => {
        const pg = pickWire(g, DISPLAY_GROUP_KEYS)
        return {
          metric: asStrOrNull(pg.metric),
          period_id: asStrOrNull(pg.period_id),
          n: asNumOrNull(pg.n),
          median: asScalar(pg.median),
          basis: asStrOrNull(pg.basis),
          members: pickWireList(pg.members, DISPLAY_MEMBER_KEYS).map((m) => ({
            handle: asStrOrNull(m.handle),
            product_id: asStrOrNull(m.product_id),
            value: asScalar(m.value),
          })),
          excluded: pickWireList(pg.excluded, DISPLAY_EXCLUDED_KEYS).map((e) => ({
            handle: asStrOrNull(e.handle),
            product_id: asStrOrNull(e.product_id),
            state: asStrOrNull(e.state),
          })),
        }
      })
    : []
  const rawDirs = isObj(rec.direction_counts) && Object.keys(rec.direction_counts).length > 0
    ? rec.direction_counts
    : null
  const direction_counts: DisplayDirections | null = rawDirs
    ? (() => {
        const t = pickWire(rawDirs, DISPLAY_DIRS_KEYS)
        return {
          up: asNumOrNull(t.up),
          down: asNumOrNull(t.down),
          flat: asNumOrNull(t.flat),
          n: asNumOrNull(t.n),
          basis: asStrOrNull(t.basis),
          other: pickWireList(t.other, DISPLAY_OTHER_KEYS).map((o) => ({
            handle: asStrOrNull(o.handle),
            state: asStrOrNull(o.state),
            note: asStrOrNull(o.note),
          })),
        }
      })()
    : null
  const recomputed: DisplayRecomputed = {
    median_by_group: groups,
    direction_counts,
    ranks: pickWireList(rec.ranks, DISPLAY_RANK_KEYS).map((r) => ({
      product_id: asStrOrNull(r.product_id),
      handle: asStrOrNull(r.handle),
      metric: asStrOrNull(r.metric),
      period_id: asStrOrNull(r.period_id),
      value: asScalar(r.value),
      set_n: asNumOrNull(r.set_n),
      rank_desc: asNumOrNull(r.rank_desc),
      rank_asc: asNumOrNull(r.rank_asc),
      basis: asStrOrNull(r.basis),
    })),
    tool_returned: pickWireList(rec.tool_returned, DISPLAY_TOOLMEDIAN_KEYS).map((t) => ({
      metric: asStrOrNull(t.metric),
      period_id: asStrOrNull(t.period_id),
      product_ids: asStrArr(t.product_ids),
      unit: asStrOrNull(t.unit),
      handle: asStrOrNull(t.handle),
      ref: asStrOrNull(t.ref),
      median: asScalar(t.median),
      label: asStrOrNull(t.label),
    })),
    // 分组合计透传（键面闭合重建；非数值/畸形计数降级为空，不猜值）
    group_totals: pickWireList(rec.group_totals, DISPLAY_GROUP_TOTAL_KEYS).map((t) => ({
      category: asStrOrNull(t.category),
      metric: asStrOrNull(t.metric),
      unit: asStrOrNull(t.unit),
      base_period_id: asStrOrNull(t.base_period_id),
      target_period_id: asStrOrNull(t.target_period_id),
      subjects_n: asNumOrNull(t.subjects_n),
      comparable_n: asNumOrNull(t.comparable_n),
      non_comparable_n: asNumOrNull(t.non_comparable_n),
      non_comparable_by_state: asStateCounts(t.non_comparable_by_state),
      base_sum: asScalar(t.base_sum),
      target_sum: asScalar(t.target_sum),
      absolute_change: asScalar(t.absolute_change),
      percent_change: asScalar(t.percent_change),
      percent_change_state: asStrOrNull(t.percent_change_state),
      sum_scope: asStrOrNull(t.sum_scope),
      basis: asStrOrNull(t.basis),
    })),
    // 集合块的工具权威整体排名透传（键面闭合重建；行为空=不产榜单，不猜）
    rankings: pickWireList(rec.rankings, DISPLAY_RANKING_KEYS).map((rk) => ({
      kind: asStrOrNull(rk.kind),
      kind_name: asStrOrNull(rk.kind_name),
      metric: asStrOrNull(rk.metric),
      unit: asStrOrNull(rk.unit),
      period_id: asStrOrNull(rk.period_id),
      window: asFactWindow(rk.window),
      selected_n: asNumOrNull(rk.selected_n),
      usable_n: asNumOrNull(rk.usable_n),
      missing_n: asNumOrNull(rk.missing_n),
      product_ids: asStrArr(rk.product_ids),
      missing_group: pickWireList(rk.missing_group, DISPLAY_RANKING_MISSING_KEYS).map((m) => ({
        product_id: asStrOrNull(m.product_id),
        value: asScalar(m.value),
        state: asStrOrNull(m.state),
        reason: asStrOrNull(m.reason),
      })),
      handle: asStrOrNull(rk.handle),
      ref: asStrOrNull(rk.ref),
      rows: pickWireList(rk.rows, DISPLAY_RANKING_ROW_KEYS).map((r) => ({
        rank: asNumOrNull(r.rank),
        product_id: asStrOrNull(r.product_id),
        value: asScalar(r.value),
      })),
    })),
  }

  const ver = isObj(v.verification) ? v.verification : {}
  const refs = pickWire(ver.refs, DISPLAY_REFS_KEYS)
  const verification: DisplayVerification = {
    refs: {
      total: asNumOrNull(refs.total),
      verified_n: asNumOrNull(refs.verified_n),
      unresolved: asStrArr(refs.unresolved),
    },
  }

  return {
    index: v.index,
    type: v.type,
    display,
    verified_facts: facts,
    recomputed,
    verification,
    data_issues: asStrArr(v.data_issues),
    cohort: filterCohort(v.cohort),
  }
}

/**
 * 投影降级的**块级最小处理**（2026-10-20 可读性收口）：单个块形状不合法时只跳过该块，
 * 保留其余形状合法且独立核验的业务块；仅当没有任何可用块时整份返回 null（缺失/null=旧 SSE
 * 的正常兼容路径）。
 *
 * 安全性依据：每个 `display_blocks` 块的数据面（`verified_facts`/`recomputed`/`cohort`）
 * 由服务端**逐块独立送检与回查**，块之间没有必须整体校验的数据完整性依赖；畸形块本身
 * 不可展示，跳过它不会改动其他块的读数。既往「任一块不合法即整份不可用」会把一个局部
 * 形状错误放大成全部结构化数据消失（只剩文字回答），此处收窄为局部跳过并保留开发日志。
 * 形状合法的空壳块（没有任何可呈现业务结果）不在此层判有效性——由
 * `lib/display-blocks.hasPresentableResult` 在视图层收口。
 */
function filterDisplayBlocks(v: unknown): DisplayBlocks | null {
  if (!isObj(v) || v.schema !== DISPLAY_BLOCKS_SCHEMA) return null
  if (!isStr(v.run_id) || !isStr(v.parse_status) || !Array.isArray(v.blocks) || v.blocks.length === 0) {
    return null
  }
  const kept = v.blocks.map(filterDisplayBlock).filter((b): b is DisplayBlock => b !== null)
  if (kept.length === 0) {
    console.warn('[R02] display_blocks 中没有形状合法的块，整份投影不可用（已降级到完整正文）')
    return null
  }
  if (kept.length !== v.blocks.length) {
    console.warn(
      `[R02] display_blocks 中 ${v.blocks.length - kept.length} 个块形状不符合同，已跳过该块；` +
      `保留其余 ${kept.length} 个有效块（已核验数据不受影响）`)
  }
  return {
    schema: DISPLAY_BLOCKS_SCHEMA,
    run_id: v.run_id,
    parse_status: v.parse_status,
    blocks: kept,
  }
}

/**
 * 键面守卫：白名单事件剥除白名单外键；白名单外事件类型整帧丢弃。
 * citations 按 {handle,state} 白名单收口。
 */
export function filterEvent(ev: { event?: string; data?: unknown }): WireEvent | null {
  const type = String(ev?.event ?? '')
  const keys = EVENT_KEYS[type]
  if (!keys) return null
  const d = (ev?.data ?? {}) as Record<string, unknown>
  const out: Record<string, unknown> = {}
  for (const k of keys) if (k in d) out[k] = (d as Record<string, unknown>)[k]
  if (type === 'run_finished') {
    if (Array.isArray(out.citations)) {
      out.citations = out.citations
        .filter((c) => c && typeof c === 'object' && 'handle' in c && 'state' in c)
        .map((c) => ({ handle: String((c as Record<string, unknown>).handle), state: String((c as Record<string, unknown>).state) }))
    }
    if ('report' in out) out.report = filterReport(out.report)
    if ('knowledge_sources' in out) out.knowledge_sources = filterKnowledgeSources(out.knowledge_sources)
    if ('evidence_details' in out) out.evidence_details = filterEvidenceDetails(out.evidence_details)
    if ('display_blocks' in out) out.display_blocks = filterDisplayBlocks(out.display_blocks)
    // 实际查询范围投影无效时置空，不推测缺失成员。
    if ('query_scope' in out) out.query_scope = filterQueryScope(out.query_scope)
  }
  return { event: type, data: out }
}
