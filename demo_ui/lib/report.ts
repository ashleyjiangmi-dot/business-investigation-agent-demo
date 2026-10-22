/**
 * 终答投影的呈现层解析：把模型生成的 Markdown 按标题切成章节，选出默认视图要显示的
 * 章节与条目数。短引用 `[E#]` 保留；长内部 handle 只在摘要视图压缩，用户可见清单不再展示
 * 完整标识（证据清单只给轻量编号入口，完整引用标识保留在后端与依据详情的「原始引用」）。
 * 标题只在能可靠归类时进入摘要，未归类章节始终保留在完整调查正文中。
 *
 * answer_md 已是经过清洗和证据核验处理的业务正文。
 */
export type Block = { level: number; title: string; body: string }

/* ====================  §行为5： 局部隔离标记的 UI 只读镜像 ==================== */

/** 局部隔离标记字面。owner＝`core/evidence_contract.py::ISOLATION_MARKER`（ 落点），
 * 此处只读镜像、不产生标记；两侧字面必须一致（verify_ix04_result_simplify.cjs 对账）。 */
export const ISOLATION_MARKER = '〔局部提示：此处含未通过核验的引用，相关内容按未核验对待〕'

/** 该段正文是否被  标记过存在引用问题——默认展示位据此让位（§行为5），不做语义判断。 */
export function hasUnverifiedMarker(text: string): boolean {
  return typeof text === 'string' && text.includes(ISOLATION_MARKER)
}

/** 去掉标题里的括号注解与空白，用于关键词匹配（不用于展示）。 */
function normalizeTitle(t: string): string {
  return t.replace(/[（(][^）)]*[）)]/g, '').replace(/\s+/g, '').trim()
}

const HEADING = /^(#{1,6})\s+(.*)$/

/** 标题 → 有序章节块（标题前若有正文，归入一个匿名首块）。 */
export function splitSections(md: string): Block[] {
  const blocks: Block[] = []
  let cur: Block = { level: 0, title: '', body: '' }
  for (const line of String(md ?? '').replace(/\r\n/g, '\n').split('\n')) {
    const h = HEADING.exec(line)
    if (h) {
      if (cur.title || cur.body.trim()) blocks.push(cur)
      cur = { level: h[1].length, title: h[2].trim(), body: '' }
    } else {
      cur.body += line + '\n'
    }
  }
  if (cur.title || cur.body.trim()) blocks.push(cur)
  return blocks
}

type Role = 'conclusion' | 'numbers' | 'boundary' | 'actions' | 'evidence' | 'other'

/** 标题只归一个角色；边界标题优先于泛化的结论标题。 */
function roleOf(b: Block): Role {
  const t = normalizeTitle(b.title)
  if (!t) return 'other'
  if (t.includes('证据核验') || t.includes('证据清单')) return 'evidence'
  if (t.includes('边界') || t.includes('口径') || /(?:能否|是否)(?:认定|判断|确认|归因)/.test(t)) return 'boundary'
  if (t.includes('结论')) return 'conclusion'
  if (t.includes('关键数值') || t.includes('核心数值') || t.includes('数值')) return 'numbers'
  if (t.includes('优先处理') || t.includes('下一步') || t.includes('建议') || t.includes('处理')) return 'actions'
  return 'other'
}

/**
 * 只压缩长内部 handle；可读的 `[E#]` 引用保留，供用户与证据清单互相查找。
 * 换行保留（段落切分依赖它）。
 */
export function stripInternal(s: string): string {
  return s
    .replace(/(pqev|pev|qev|ev):[A-Za-z0-9_.:-]*/g, '')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/[ \t]+\n/g, '\n')
    .trim()
}

const isListItem = (line: string) => /^\s*(?:[-*]|\d+[.)、])\s+/.test(line)
const itemBody = (line: string) => line.replace(/^\s*(?:[-*]|\d+[.)、])\s+/, '').trim()

/** 段落切分（连续空行分段）；段内原有换行与项目符号原样保留。 */
function paragraphs(body: string): string[] {
  return body
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter(Boolean)
}

/**
 * 条目序列：块内确有项目符号时逐条取，否则按段落取。
 * 不拆单行内的换行，避免把模型原文改写成两句。
 */
function items(body: string): string[] {
  const lines = body.split('\n').map((l) => l.trim()).filter(Boolean)
  const listed = lines.filter(isListItem).map(itemBody).filter(Boolean)
  if (listed.length) return listed
  return paragraphs(body)
}

export type TableRow = { cells: string[]; raw: string }

/** Markdown 表格：跳过分隔行与表头行（表头文字原样在完整正文里可见）。 */
export function tableRows(body: string): TableRow[] {
  const rows: TableRow[] = []
  for (const line of body.split('\n')) {
    const t = line.trim()
    if (!t.startsWith('|')) continue
    if (/^\|[\s:|-]+\|?$/.test(t)) continue
    const cells = t.replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim())
    rows.push({ cells, raw: t })
  }
  return rows.slice(1)
}

/** 一条关键发现=原章节里一行/一条目的**逐格拆分**（表格按列、条目按整句），不改写文字 */
export type Finding = { parts: string[] }

export type CompactReport = {
  /** 结论段（模型原文，短引用保留；解析不出=null */
  conclusion: string | null
  /** 关键发现：来自「关键数值」章节，表格行=单元格数组（原样搬用）、无表格时=条目数组 */
  findings: Finding[]
  /** 表格章节中位于表格外的完整 Markdown 说明，按原顺序保留 */
  findingNotes: string[]
  /** 超过默认 3 条、仍可在完整调查中查看的发现数 */
  findingOverflow: number
  actions: string[]
  /** 超过默认 2 条、仍可在完整调查中查看的下一步数 */
  actionOverflow: number
  boundary: string[]
  /** 没有可靠主结论时=true，页面显示整份正文兜底 */
  unparsed: boolean
  /** 章节未解析出时显示完整业务正文。 */
  fallbackText: string
  /** 未归类章节标题；卡片会提示这些内容在完整调查中保留 */
  skipped: string[]
  /** 完整业务正文原文。 */
  full: string
  businessText: string
}

const FINDING_MAX = 3
const ACTION_MAX = 2

/**
 * 规范业务正文 → 精简卡片模型。纯函数，不抛异常；无法可靠归类时业务正文整份兜底。
 *
 */
export function parseReport(md: string): CompactReport {
  const full = String(md ?? '')
  const blocks = splitSections(full)
  let conclusion: string | null = null
  let fallbackText = ''
  let findings: Finding[] = []
  let findingNotes: string[] = []
  let actions: string[] = []
  let boundary: string[] = []
  const skipped: string[] = []

  for (const b of blocks) {
    const role = roleOf(b)
    switch (role) {
      case 'evidence':
        break
      case 'conclusion': {
        const ps = paragraphs(b.body).filter((p) => p !== '---')
        if (ps.length) {
          const text = stripInternal(ps.join('\n\n'))
          conclusion = [conclusion, text].filter(Boolean).join('\n\n')
        }
        else skipped.push(b.title)
        break
      }
      case 'numbers': {
        const rows = tableRows(b.body)
        let sectionFindings: Finding[]
        if (rows.length) {
          sectionFindings = rows.map((r) => ({ parts: r.cells.map(stripInternal).filter(Boolean) }))
          let outsideTable: string[] = []
          for (const line of b.body.split('\n')) {
            if (line.trim().startsWith('|')) {
              const note = outsideTable.join('\n').trim()
              if (note) findingNotes.push(note)
              outsideTable = []
            } else {
              outsideTable.push(line)
            }
          }
          const trailingNote = outsideTable.join('\n').trim()
          if (trailingNote) findingNotes.push(trailingNote)
        } else {
          sectionFindings = items(b.body).map((line) => ({ parts: [stripInternal(line)] }))
        }
        findings = [...findings, ...sectionFindings]
        if (!sectionFindings.length) skipped.push(b.title)
        break
      }
      case 'boundary': {
        const t = normalizeTitle(b.title)
        const titleContainsDecision = /(?:能否|是否)(?:认定|判断|确认|归因)|(?:结论|判断)边界/.test(t) && /[:：]/.test(b.title)
        const sectionBoundary = [
          ...(titleContainsDecision ? [stripInternal(b.title)] : []),
          ...items(b.body).map(stripInternal).filter(Boolean),
        ]
        boundary = [...boundary, ...sectionBoundary]
        if (!sectionBoundary.length) skipped.push(b.title)
        break
      }
      case 'actions': {
        const sectionActions = items(b.body).map(stripInternal).filter(Boolean)
        actions = [...actions, ...sectionActions]
        if (!sectionActions.length) skipped.push(b.title)
        break
      }
      default:
        if (b.title) skipped.push(b.title)
    }
  }

  const findingOverflow = Math.max(0, findings.length - FINDING_MAX)
  const actionOverflow = Math.max(0, actions.length - ACTION_MAX)
  findings = findings.slice(0, FINDING_MAX)
  actions = actions.slice(0, ACTION_MAX)

  // 无可靠结论章节时显示完整业务正文。
  const unparsed = !conclusion
  if (unparsed) {
    // 兜底显示规范业务正文；只压缩长 handle，短 [E#] 引用原样保留。
    fallbackText = stripInternal(full)
    conclusion = null
  }
  return {
    conclusion,
    findings,
    findingNotes,
    findingOverflow,
    actions,
    actionOverflow,
    boundary,
    unparsed,
    fallbackText,
    skipped,
    full,
    businessText: full,
  }
}

// —— 证据清单：run_finished.citations → 业务可读行——

export const QUERY_OPS: Record<string, string> = {
  value: '单期取值',
  object: '单期取值',
  diff: '两期对比',
  set_compare: '集合对比',
  states: '身份与记录状态',
  state: '身份与记录状态',
  object_state: '身份与记录状态',
  compare: '集合对照',
  compare_row: '集合对照行',
  category_total: '品类汇总',
  //  日期窗口引用（kind 取自 canonical 引用键形；旧周期引用词表不变）
  window: '日期窗口覆盖',
  window_object: '日期窗口取值',
  window_diff: '日期窗口对比',
  window_compare: '日期窗口集合对照',
  window_compare_row: '日期窗口集合对照行',
  window_compare_missing: '日期窗口集合缺失行',
  day_series: '连续日读数',
}

export type HandleInfo = {
  ns: string
  queryId: string
  op: string
  opLabel: string
  productId: string
  periodId: string
  metric: string
  parseFailed: boolean
}

/** `pqev:p1:object:10010408340172:period_demo_p1:M1`｜`pqev:p1:diff:<pid>:M1:ok`｜`pev:<pid>:<period>` */
export function parseHandle(handle: string): HandleInfo {
  const parts = String(handle ?? '').split(':')
  const info: HandleInfo = {
    ns: parts[0] ?? '',
    queryId: '',
    op: '',
    opLabel: '',
    productId: '',
    periodId: '',
    metric: '',
    parseFailed: parts.length < 2,
  }
  for (const p of parts.slice(1)) {
    if (!p) continue
    if (/^p\d+$/.test(p)) info.queryId = info.queryId || p
    else if (/^period_/.test(p)) info.periodId = info.periodId || p
    else if (/^M\d$/.test(p)) info.metric = info.metric || p
    else if (/^\d{8,}$/.test(p)) info.productId = info.productId || p
    else if (!info.op) info.op = p
  }
  info.opLabel = QUERY_OPS[info.op] ?? info.op
  return info
}

export type EvidenceRow = {
  /** answer_md 中与此 handle 对应的短引用号；用于对照正文里的 [E#] */
  ref: string
  handle: string
  metric: string
  productId: string
  periodId: string
  /** handle 里的取数方式原样 token（只用于与调查步骤做族匹配，不显示） */
  op: string
  opLabel: string
  ns: string
}

export type EvidenceList = {
  ok: boolean | null
  accepted: EvidenceRow[]
  rejected: string[]
  hiddenInAnswer: number | null
  rows: EvidenceRow[]
  guardDetail: string
}

/**
 * 引用核验装配：run_finished.citations=[{handle,state}] → accepted/rejected 分类。
 * 结果页只用 rejected 计数给一句可信提示（不再渲染证据清单表格）；引用详情由
 * `evidence_details` 单点驱动。answerMarkdown 中的 `[E#]→handle` 映射保留用于离线保真校验。
 * hiddenInAnswer=run_finished.hidden_in_answer（正文中被隐藏的未通过核验引用数）。
 */
export function evidenceFromCitations(
  citations: unknown,
  hiddenInAnswer: number | null,
  answerMarkdown = '',
): EvidenceList | null {
  if (!Array.isArray(citations)) return null
  const referenceByHandle = new Map<string, string>()
  for (const match of String(answerMarkdown).matchAll(/\[(E\d+)\]\s*(?:→|->)\s*((?:pqev|pev|qev|ev):[A-Za-z0-9_.:-]+)/g)) {
    referenceByHandle.set(match[2], match[1])
  }
  const acceptedRaw: string[] = []
  const rejected: string[] = []
  for (const c of citations) {
    if (!c || typeof c !== 'object') continue
    const { handle, state } = c as { handle?: unknown; state?: unknown }
    if (typeof handle !== 'string' || !handle) continue
    if (state === 'accepted') acceptedRaw.push(handle)
    else if (state === 'rejected') rejected.push(handle)
  }
  const row = (handle: string): EvidenceRow => {
    const h = parseHandle(handle)
    return {
      ref: referenceByHandle.get(handle) ?? '',
      handle,
      metric: h.metric,
      productId: h.productId,
      periodId: h.periodId,
      op: h.op,
      // 表格里只显示中文取数方式；未收录的 op 留空，不把内部 token 端到页面上
      opLabel: QUERY_OPS[h.op] ?? (h.ns === 'pev' ? '单期取证' : ''),
      ns: h.ns,
    }
  }
  const accepted = acceptedRaw.map(row)
  return {
    ok: null,
    accepted,
    rejected,
    hiddenInAnswer,
    rows: accepted,
    guardDetail: '',
  }
}

/** 完整正文中只折叠长 handle；完整引用仍保留在各依据详情的「原始引用」内（本函数供离线保真校验/兼容面）。 */
export function compactEvidenceHandles(markdown: string, evidence: EvidenceList | null): string {
  let text = String(markdown ?? '')
  for (const row of evidence?.rows ?? []) {
    if (!/^(?:pqev|pev|qev|ev):/.test(row.handle)) continue
    text = text.replaceAll(row.handle, '（完整引用标识见证据清单）')
  }
  return text
}
