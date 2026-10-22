/**
 * 将结构化报告映射为页面视图，并检查摘要与详情是否一致。
 * 无报告时使用正文；不一致时展示完整详情，不拼接互相冲突的摘要。
 * 按条目形状选择指标卡、商品比较表或候选列表；无法识别的条目保留为正文。
 * 仅提取原文中的名称、数值和引用，不计算指标或产生经营判断。
 * 摘要与下一步按展示上限折叠，必要限制全部可见；完整表格不拆分。
 */
import type { ReportV1 } from './trace-filter'
import type { TraceScope } from './trace'

export const FINDING_MAX = 3
export const STEP_MAX = 2

/* ==================== 形状分类 ==================== */

export type ChangeItem = {
  raw: string
  /** 冒号前的主语（名称/指标名，含原文里的加粗与 ID） */
  name: string
  baseLabel: string
  base: string
  unitBase: string
  targetLabel: string
  target: string
  unitTarget: string
  /** 绝对变化（逐字，不含括号）；缺=空串 */
  change: string
  /** 百分比幅度（逐字，如 -71.11%）；缺=空串 */
  rate: string
  /** 方向 token（上升/下降/持平/up/down/flat），原文有才填 */
  direction: string
  /** 引用短号串（如 " [E2] [E12]"），逐字 */
  refs: string
  /** 变化之后的余句（入选条件/依据），逐字；空=普通变化行 */
  basis: string
}

export type MdTable = {
  raw: string
  /** 表头行单元（逐字）；无表头时为空数组 */
  headers: string[]
  /** 数据行（已去分隔行与表头行） */
  rows: string[][]
}

export type FindingUnit =
  | { kind: 'md-table'; table: MdTable }
  /** 表格与普通段落混排的条目：整条以完整 Markdown 呈现（不拆表、不丢表格外说明） */
  | { kind: 'md-block'; text: string }
  | { kind: 'metric-cards'; items: ChangeItem[] }
  | { kind: 'compare-table'; items: ChangeItem[] }
  | { kind: 'candidates'; items: ChangeItem[] }
  | { kind: 'plain'; text: string }

const NUM = String.raw`[+\-]?\d[\d,]*(?:\.\d+)?(?:[万亿])?`
const UNIT = String.raw`(?:\s*(?:单/千次播放|次|单|条|元|倍))?`
const LB = String.raw`(?<labelBase>上一周期|本周期|基期|对比期|上期|本期)?`
const LT = String.raw`(?<labelTarget>上一周期|本周期|基期|对比期|上期|本期)?`
const REFS = String.raw`(?:\s*\[E\d+\])*`

/**
 * 变化行：`名称：[标签]数值[单位] → [标签]数值[单位]（…）…余句。`
 * 全锚定（^…$，单行）：任何未被这些片段消费掉的文本要么进 refs/basis，要么整条不匹配。
 */
const CHANGE_RE = new RegExp(
  String.raw`^(?<name>[^：:\n]{1,80}?)\s*[：:]\s*` +
    `${LB}\\s*(?<base>${NUM})(?<unitBase>${UNIT})\\s*(?:→|->)\\s*` +
    `${LT}\\s*(?<target>${NUM})(?<unitTarget>${UNIT})` +
    String.raw`(?:\s*[，,;；]\s*(?:变化|绝对差)?\s*(?<chg>${NUM})(?<chgUnit>${UNIT}))?` +
    String.raw`(?:\s*[（(](?<paren>[^（）\n]{1,60})[）)])?` +
    String.raw`(?:\s*[，,;；]?\s*方向\s*(?<dir>下降|上升|持平|down|up|flat))?` +
    `(?<refs>${REFS})` +
    String.raw`(?<basis>[^\n]*?)\s*。?\s*$`,
)

/** 括号内容拆分：`-3,200 单，-71.11%` → 变化+幅度；`-71.11%` → 幅度；其余整体算变化。 */
function splitParen(paren: string): { change: string; rate: string } {
  const parts = paren.split(/[，,]/).map((p) => p.trim()).filter(Boolean)
  if (parts.length >= 2 && /^[+\-]?[\d,.]+%$/.test(parts[parts.length - 1])) {
    return { change: parts.slice(0, -1).join('，'), rate: parts[parts.length - 1] }
  }
  if (parts.length === 1 && /^[+\-]?[\d,.]+%$/.test(parts[0])) {
    return { change: '', rate: parts[0] }
  }
  return { change: paren.trim(), rate: '' }
}

export function parseChangeItem(raw: string): ChangeItem | null {
  const m = CHANGE_RE.exec(raw)
  if (!m?.groups) return null
  const g = m.groups as unknown as Record<string, string>
  const paren = splitParen(g.paren ?? '')
  // 括号与「变化 …」同时出现时以「变化 …」为准、括号只取幅度；两者都是原文子串。
  const change = g.chg ? `${g.chg}${g.chgUnit ?? ''}`.trim() : paren.change
  return {
    raw,
    name: g.name.trim(),
    baseLabel: g.labelBase ?? '',
    base: g.base,
    unitBase: g.unitBase ?? '',
    targetLabel: g.labelTarget ?? '',
    target: g.target,
    unitTarget: g.unitTarget ?? '',
    change,
    rate: paren.rate,
    direction: g.dir ?? '',
    refs: g.refs ?? '',
    basis: (g.basis ?? '').replace(/^[，,;；]\s*/, '').trim(),
  }
}

/**
 * Markdown 表格条目：仅当**整条**都是竖线行（非空行全部 `|` 开头，≥2 行且含分隔行）才转换；
 * 表头取首行，分隔行丢弃。表格外还带普通段落/说明文字的混排条目返回 null——
 * 由调用方整条退回完整 Markdown，不丢表格外原文。
 */
export function parseMdTable(raw: string): MdTable | null {
  const nonEmpty = raw.split('\n').map((l) => l.trim()).filter(Boolean)
  if (nonEmpty.length < 2 || !nonEmpty.every((l) => l.startsWith('|'))) return null
  if (!nonEmpty.some((l) => /^\|[\s:|-]+\|?$/.test(l))) return null
  const cellsOf = (line: string) =>
    line.replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim())
  const all = nonEmpty.filter((l) => !/^\|[\s:|-]+\|?$/.test(l)).map(cellsOf)
  return { raw, headers: all[0] ?? [], rows: all.slice(1) }
}

/** 条目含竖线表格体（≥2 行且含分隔行）但整体不是纯表格——表格之外还有别的文字。 */
function hasMdTableFragment(raw: string): boolean {
  const pipe = raw.split('\n').map((l) => l.trim()).filter((l) => l.startsWith('|'))
  return pipe.length >= 2 && pipe.some((l) => /^\|[\s:|-]+\|?$/.test(l))
}

/** 主语是否指标行：含 M1–M4 代号（形状判断；代号与中文名由服务端目录负责映射）。 */
function isMetricSubject(name: string): boolean {
  return /(^|[^A-Za-z0-9])M[1-4]([^A-Za-z0-9]|$)/.test(name)
}

/** 主语是否商品行：含 8 位以上 ID，或逐字命中服务端下发目录的商品名/消歧标签。 */
function isProductSubject(name: string, scope: TraceScope): boolean {
  if (/\d{8,}/.test(name)) return true
  const plain = name.replace(/[（(][0-9A-Za-z_]+[)）]/g, '')
  return scope.products.some((p) => {
    const label = p.scope_label ?? p.product_name
    return label && (plain.includes(label) || name.includes(label))
  })
}

type Bucket = 'md-table' | 'md-block' | 'metric' | 'product' | 'candidate' | 'plain'

function classify(raw: string, scope: TraceScope): { bucket: Bucket; item: ChangeItem | null } {
  const table = parseMdTable(raw)
  if (table) return { bucket: 'md-table', item: null }
  if (raw.includes('\n')) {
    return hasMdTableFragment(raw) ? { bucket: 'md-block', item: null } : { bucket: 'plain', item: null }
  }
  const item = parseChangeItem(raw)
  if (!item) return { bucket: 'plain', item: null }
  if (item.basis) {
    return isProductSubject(item.name, scope)
      ? { bucket: 'candidate', item }
      : { bucket: 'plain', item: null }
  }
  if (isMetricSubject(item.name)) return { bucket: 'metric', item }
  if (isProductSubject(item.name, scope)) return { bucket: 'product', item }
  return { bucket: 'plain', item: null }
}

/**
 * 条目序列 → 展示单元序列（保持原顺序；只合并**连续**同类条目）。
 * 单条商品行不强行成表：退回普通正文（宁缺毋滥，不制造第二形状）。
 * `suppressCards`＝本 run 有 display_blocks 类型化展示——metric/compare/candidate
 * 三类卡片桶退普通正文（同一结果不并排出现两套卡片；md-table/md-block 等正文形状保留）。
 */
export function groupFindings(
  items: string[],
  scope: TraceScope,
  opts?: { suppressCards?: boolean },
): FindingUnit[] {
  type Run = { bucket: Bucket; items: ChangeItem[]; raws: string[] }
  const runs: Run[] = []
  let cur: Run | null = null
  for (const raw of items) {
    const { bucket, item } = classify(raw, scope)
    if (cur && cur.bucket === bucket) {
      if (item) cur.items.push(item)
      cur.raws.push(raw)
    } else {
      cur = { bucket, items: item ? [item] : [], raws: [raw] }
      runs.push(cur)
    }
  }
  const units: FindingUnit[] = []
  const suppressed = opts?.suppressCards
  for (const run of runs) {
    switch (run.bucket) {
      case 'md-table':
        for (const raw of run.raws) units.push({ kind: 'md-table', table: parseMdTable(raw)! })
        break
      case 'md-block':
        for (const raw of run.raws) units.push({ kind: 'md-block', text: raw })
        break
      case 'metric':
        if (suppressed) for (const raw of run.raws) units.push({ kind: 'plain', text: raw })
        else units.push({ kind: 'metric-cards', items: run.items })
        break
      case 'product':
        if (suppressed) for (const raw of run.raws) units.push({ kind: 'plain', text: raw })
        else if (run.items.length >= 2) units.push({ kind: 'compare-table', items: run.items })
        else units.push({ kind: 'plain', text: run.raws[0] })
        break
      case 'candidate':
        if (suppressed) for (const raw of run.raws) units.push({ kind: 'plain', text: raw })
        else units.push({ kind: 'candidates', items: run.items })
        break
      default:
        for (const raw of run.raws) units.push({ kind: 'plain', text: raw })
    }
  }
  return units
}

/** 渲染两期读数用的逐字拼接（标签+数值+单位+箭头）；全部子串，不算数。 */
export function readingOf(it: ChangeItem): string {
  const left = `${it.baseLabel ? it.baseLabel + ' ' : ''}${it.base}${it.unitBase}`.trim()
  const right = `${it.targetLabel ? it.targetLabel + ' ' : ''}${it.target}${it.unitTarget}`.trim()
  return `${left} → ${right}`
}

/* ==================== 一致性守卫与视图模型 ==================== */

const norm = (s: string): string => s.replace(/\s+/g, ' ').trim()

const LIST_LINE = /^\s*(?:[-*]|\d+[.)、])\s+/

/** Markdown 表格分隔行：`| --- | ---: |`。 */
const TABLE_RULE = /^\|[\s:|-]+\|?$/
const isPipeLine = (line: string) => line.startsWith('|')

/**
 * 完整 Markdown 表格的起点：表头行 + **紧跟**的分隔行。
 * 表头与分隔行之间不要求空行——服务端 summary 常把表格直接接在结论句后（同一块内）。
 */
function startsMdTable(lines: string[], i: number): boolean {
  return isPipeLine(lines[i] ?? '') && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])
}

/**
 * 服务端 `summary` 用单个换行衔接句子与列表项、段间常无空行（/ 真机与回放实测），
 * 故按**形状**（行结构与 Markdown 表格/列表标记，不读语义、不看关键词）切显示段——只按空行
 * 分块会把整条结论挤成一个显示段，渲染后成一块密集文本墙、「前 2 段可见、其余折叠」失效。
 * 一字不删不改，段间顺序保持：
 * 1. 先按空行分块；
 * 2. 块内遇到完整 Markdown 表格（表头 + 分隔行 + 连续竖线行）→ 整表收成一个**不可拆分**的
 *    显示段（表头/分隔行/数据行不再各自成段，避免表格被折叠边界截断、渲染成裸竖线）；
 *    表格前的结论句照常先成段——即使原文没有空行也把结论文本与表格分开；
 * 3. 块内列表项行（`- ` / `* ` / `1.` / `1、`）开启新显示段；
 * 4. 以冒号结尾的引导句粘住其**紧跟的第一条**列表项（不把「只有 1 个：」与答案切开）；
 * 5. 普通行（含列表项之后的清单外补充句）各自另起一段——单换行衔接的就是原文相邻段落，
 *    留在同段会重新挤成文本墙。
 * 段内原文换行保留（仅存在于冒号引导句粘段场景），由渲染层以换行显示。
 */
export function splitSummaryParas(summary: string): string[] {
  const out: string[] = []
  for (const chunk of summary.split(/\n\s*\n/)) {
    const p = chunk.trim()
    if (!p) continue
    const lines = p.split('\n').map((l) => l.trim()).filter(Boolean)
    if (lines.length === 1) { out.push(lines[0]); continue }
    let cur = ''
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i]
      // 完整表格整块取出：表头 + 分隔行 + 连续数据行；表格前的正文单独成段
      if (startsMdTable(lines, i)) {
        if (cur) { out.push(cur); cur = '' }
        const block: string[] = [line]
        i++
        while (i < lines.length && isPipeLine(lines[i])) { block.push(lines[i]); i++ }
        i--
        out.push(block.join('\n'))
        continue
      }
      if (!cur) { cur = line; continue }
      const curIsItem = LIST_LINE.test(cur.split('\n').pop() ?? '')
      if (LIST_LINE.test(line)) {
        // 冒号引导句粘住紧跟的第一条列表项；后续列表项各自成段
        if (/[：:]$/.test(cur) && !curIsItem) { cur += '\n' + line; continue }
        out.push(cur); cur = line; continue
      }
      // 列表清单结束后的普通句另起一段（不把清单外说明粘进前一组）
      if (curIsItem) { out.push(cur); cur = line; continue }
      // 普通行各自成段：相邻普通行＝原文相邻段落，合并会重新挤成文本墙并压死折叠机制
      out.push(cur); cur = line
    }
    if (cur) out.push(cur)
  }
  return out
}

/* ==================== IX04R：必要限制分层（业务限制就近 / 取数细节按需） ==================== */

/**
 * IX04R 经营结果页可读性：必要限制的首屏只留**影响本轮判断的业务限制**（范围、可比性、
 * 未知项、替代比较），把两类「说明性文字」挪进按需折叠区——页面级已统一给出 Demo 口径，
 * 限制段不再重复自证；底层行数/字段覆盖这类取数实现细节不是业务限制。
 *
 * 判据纪律（**只判呈现位置，不删任何一条、不改写一个字**）：
 * - 判据是**数据口径与取数字面**，不是业务语义推断——不做「这条限制重不重要」的语义判断；
 * - 命中即移进折叠区、未命中一律留在首屏（宁可多留一条，也不把业务限制藏起来）；
 * - 每条限制按 `；` 切子句判「口径自证」：所有子句都只讲模拟数据身份、且整条不含任何具体
 *   业务限定词（不可比/缺失/未知/无法判定/替代对照/行数/底层）才算口径自证——带业务限定
 *   的混合句原样留在首屏；
 * - 移到折叠区的原文逐字保留（限制条数与内容都不丢），只换呈现位置。
 */
const CALIBER_IDENTITY = /(模拟|Demo|demo)/
/** 具体业务限定词：出现即视为携带业务信息，整条留在首屏（保守面）。 */
const CALIBER_SCOPE_GUARD =
  /(不可比|缺失|未知|无法|不支持|不升格|不构成|不代表全市场|不代表完整|对照|改用|底层|行数|行覆盖)/
/** 取数/字段实现细节：底层行数、字段覆盖、列口径实现（不是业务判断依据）。 */
const FETCH_DETAIL = /(底层\s*\d+\s*行|行覆盖|plc_[a-z_]+)/
/**
 *  问题范围自证：只声明「本轮只查了所问指标、其他指标未在本轮查询范围」的通用说明
 * （ix05 真机 ui-d965：纯读数对比题被模型自加「播放量不代表订单量、其余指标未在本轮查询
 * 范围」）。判据仍是字面不是业务语义：命中“未在本轮查询/调查范围”字样、且整条不带任何
 * 具体业务限定词（不可比/缺失/未知/无法/不支持…）才算——带业务限定的混合句原样留首屏
 * （保守面不变）。原文逐字移入折叠区，一条不删。
 */
const SCOPE_OF_QUESTION = /未(?:在|于)本轮(?:查询|调查)(?:范围|内)/

function isScopeOfQuestionRestatement(text: string): boolean {
  if (CALIBER_SCOPE_GUARD.test(text)) return false
  return SCOPE_OF_QUESTION.test(text)
}

/** 一条限制是否只是「Demo 模拟口径」自证：逐子句都只讲数据身份，且无具体业务限定。 */
function isCaliberRestatement(text: string): boolean {
  const clauses = text.split(/[；;]/).map((c) => c.trim()).filter(Boolean)
  if (!clauses.length) return false
  if (CALIBER_SCOPE_GUARD.test(text)) return false
  return clauses.every((c) => CALIBER_IDENTITY.test(c))
}

export type CaveatSplit = {
  /** 首屏：影响本轮判断的业务限制（原序） */
  business: string[]
  /** 按需折叠区：口径自证、取数实现细节与问题范围自证（原序，逐字保留） */
  restated: string[]
}

/** 必要限制分层（原序；只决定呈现位置，条目一条不丢）。 */
export function splitCaveats(caveats: string[]): CaveatSplit {
  const business: string[] = []
  const restated: string[] = []
  for (const c of caveats) {
    if (isCaliberRestatement(c) || FETCH_DETAIL.test(c) || isScopeOfQuestionRestatement(c)) {
      restated.push(c)
    } else {
      business.push(c)
    }
  }
  return { business, restated }
}

export type StructuredView = {
  report: ReportV1
  /** 主结论仅取 report.summary；旧 answer_line 保留在输入合同中但不参与渲染或切段。 */
  summaryParas: string[]
  /** 关键发现展示单元（默认前 FINDING_MAX 条参与分组） */
  findingUnits: FindingUnit[]
  /** 超出默认条数、原样保留在折叠区的关键发现条目 */
  findingOverflow: string[]
  /** 下一步：默认可见前 STEP_MAX 条 */
  nextSteps: string[]
  /** 超出默认条数、原样保留在折叠区的下一步 */
  nextStepsOverflow: string[]
  /** 必要限制全部默认可见。 */
  caveats: string[]
}

export type StructuredOutcome =
  | { status: 'absent' }
  /** 冲突结局携带 `detail_md`：结果卡主区以完整 Markdown 直显原始有效正文，不再经过精简解析裁剪 */
  | { status: 'conflict'; mismatches: string[]; detail_md: string }
  | { status: 'structured'; view: StructuredView }

function isReportLike(r: unknown): r is ReportV1 {
  if (!r || typeof r !== 'object') return false
  const x = r as Record<string, unknown>
  return x.schema === 'oagent_report_v1'
    && typeof x.summary === 'string' && typeof x.detail_md === 'string'
    && Array.isArray(x.key_findings) && x.key_findings.every((v) => typeof v === 'string')
    && Array.isArray(x.next_steps) && x.next_steps.every((v) => typeof v === 'string')
    && Array.isArray(x.caveats) && x.caveats.every((v) => typeof v === 'string')
}

/**
 * 一致性守卫：summary / key_findings / next_steps / caveats 每条都必须是 `detail_md`
 * 的逐字子串（空白规整后）。 的解析保证成立；不成立说明上游异常或合同演进错位，
 * 按卡片口径回退完整详情。
 */
export function buildStructuredOutcome(
  report: unknown,
  scope: TraceScope,
  opts?: { suppressCardShapes?: boolean },
): StructuredOutcome {
  if (report == null || !isReportLike(report)) return { status: 'absent' }
  const r = report
  const detail = norm(r.detail_md)
  const mismatches: string[] = []
  const check = (field: string, value: string) => {
    if (!detail.includes(norm(value))) mismatches.push(`${field}: ${value.slice(0, 80)}`)
  }
  check('summary', r.summary)
  r.key_findings.forEach((v, i) => check(`key_findings[${i}]`, v))
  r.next_steps.forEach((v, i) => check(`next_steps[${i}]`, v))
  r.caveats.forEach((v, i) => check(`caveats[${i}]`, v))
  if (mismatches.length) return { status: 'conflict', mismatches, detail_md: r.detail_md }

  const shown = r.key_findings.slice(0, FINDING_MAX)
  return {
    status: 'structured',
    view: {
      report: r,
      summaryParas: splitSummaryParas(r.summary),
      findingUnits: groupFindings(shown, scope, { suppressCards: opts?.suppressCardShapes }),
      findingOverflow: r.key_findings.slice(FINDING_MAX),
      nextSteps: r.next_steps.slice(0, STEP_MAX),
      nextStepsOverflow: r.next_steps.slice(STEP_MAX),
      caveats: [...r.caveats],
    },
  }
}
