/**
 * 为结构化商品字段生成目录名称与消歧标签，不对自由正文全局替换。
 * 只有主语包含单一商品 ID 且目录命中时才替换，否则保留原文。
 * 原文仍可通过 title 和完整详情回查。
 */
import { periodLabel, type TraceScope } from './trace'
import { cohortReadingText } from './evidence-view'

export type ProductSubjectDisplay = {
  /** 展示文本：目录命中=消歧标签；其余=原文逐字 */
  text: string
  /** 命中的商品 ID（目录未命中=null） */
  productId: string | null
  /** 是否发生了「以目录名替换含 ID 原文」的展示转换 */
  replaced: boolean
}

const ID_RE = /\d{8,}/g

/** 从主语原文确定性识别单一商品 ID；0 个或多个 ID 都不猜（返回 null）。 */
function soleProductId(name: string): string | null {
  const ids: string[] = []
  for (const m of String(name ?? '').matchAll(ID_RE)) {
    if (!ids.includes(m[0])) ids.push(m[0])
  }
  return ids.length === 1 ? ids[0] : null
}

/**
 * 结构化组件主语 → 展示文本。
 * 目录命中：显示 scope_label（同名商品自带品牌消歧；服务端单点现算，前端不另造第二套口径）。
 * 目录未命中 / 无 ID / 多 ID：原样返回（必要标识保留，不猜测）。
 */
export function productSubjectDisplay(name: string, scope: TraceScope | null | undefined): ProductSubjectDisplay {
  const raw = String(name ?? '')
  const pid = soleProductId(raw)
  if (!pid) return { text: raw, productId: null, replaced: false }
  const p = (scope?.products ?? []).find((x) => x.product_id === pid)
  if (!p) return { text: raw, productId: null, replaced: false }
  const label = p.scope_label || p.product_name
  if (!label) return { text: raw, productId: pid, replaced: false }
  // 消歧兜底：目录里同名且标签没带品牌信息（同名无法区分）时保留含 ID 原文，不猜身份
  const sameName = (scope?.products ?? []).filter((x) => x.product_name === p.product_name).length
  if (sameName > 1 && label === p.product_name) return { text: raw, productId: pid, replaced: false }
  // 目录命中：展示名统一用服务端消歧标签（原文可回查：title 与完整详情正文不变）
  return { text: label, productId: pid, replaced: raw !== label }
}

/* ---------- 运营首屏自由文本可读化 ---------- */

/**
 * 主回答 focus/why、结论、必要限制、下一步等模型自由文本的展示层可读化。
 * 只做**目录验证过的确定性映射**，不做语义改写、不做全篇宽泛替换：
 * - 商品 ID（8 位以上数字）：目录精确命中才换成消歧标签（同名商品标签含品牌；
 *   「同名且标签＝名称」的消歧不足情形保留原文——与 productSubjectDisplay 同一判定）；
 * - 指标代号（M1–M5 等独立 token）：`scope.metrics` 命中才换中文名；缺省原样保留；
 * - 周期 ID（period_* token）：`periodLabel` 目录命中才换标签；查不到原样保留（不猜日期）。
 * 邻接去重（纯字符串规则，防「名 名」重复铺开）：名称已在紧邻后文时去掉 ID 本体；
 * 紧邻后文的（…）括注与标签互为包含时去掉重复括注（模型简写「（张亮）」被消歧标签覆盖、
 * 模型注释「（商品周期订单量）」与目录名重叠）。
 * 保护面：fenced/行内代码与 Markdown 链接目标内的文本一律原样保留（不破坏代码、链接；
 * [E#] 引用短号不含长数字/指标形状，天然不受影响）。
 * 原文（含 ID/代号）回查路径：完整分析折叠区（逐字）、证据详情 product_id、运行记录。
 */
const FREE_ID_RE = /\d{8,}/g
const FREE_SHORT_ID_RE = /(?<![A-Za-z0-9_])0\d{2}(?!\d)/g
const FREE_METRIC_RE = /(?<![A-Za-z0-9_])M\d+(?![A-Za-z0-9_])/g
const FREE_PUBLISHED_RE = /\bpublished\b/gi
/** 紧邻后文的（…）括注（全角/半角，不跨嵌套）。 */
const PAREN_GLOSS_RE = /^[（(]([^（）()]+)[）)]/
const PROTECT_SPLIT_RE = /(```[\s\S]*?```|`[^`\n]*`|\[[^\]\n]*\]\([^)\n]*\))/g

/**
 * 按 re 逐个命中替换为目录标签；命中失败原样保留。
 * 替换时做邻接去重：①名称已在紧邻后文（允许空白间隔）→ 只去掉命中本体与冗余空白；
 * ②紧邻后文以（…）括注开头且括注内容与标签互为包含 → 连括注一起去掉。
 */
function replaceWithCatalogLabel(
  text: string,
  re: RegExp,
  resolve: (token: string) => string | null,
  scope: TraceScope,
  dedupeIdentityParentheses = false,
): string {
  let out = ''
  let last = 0
  re.lastIndex = 0
  for (let m = re.exec(text); m; m = re.exec(text)) {
    out += text.slice(last, m.index)
    const label = resolve(m[0])
    if (!label) {
      out += m[0]
      last = m.index + m[0].length
      continue
    }
    const rest = text.slice(m.index + m[0].length)
    if (dedupeIdentityParentheses && /[（(]\s*$/.test(text.slice(last, m.index)) && /^[）)]/.test(rest)) {
      const beforeId = text.slice(last, m.index).slice(0, -1)
      const visibleTail = beforeId.replace(/[`*_~]/g, '').replace(/[^a-zA-Z0-9\u4e00-\u9fff]/g, '')
      const nameAlreadyVisible = scope.products.some((product) => {
        if (product.product_id !== m![0]) return false
        const aliases = [product.scope_label, product.product_name].filter((v): v is string => Boolean(v))
        return aliases.some((alias) => {
          const normalized = alias.replace(/[^a-zA-Z0-9\u4e00-\u9fff]/g, '')
          return normalized !== '' && visibleTail.endsWith(normalized)
        })
      })
      if (nameAlreadyVisible) {
        out = out.slice(0, -1)
        last = m.index + m[0].length + 1
        continue
      }
    }
    const gap = /^\s*/.exec(rest)![0]
    if (rest.slice(gap.length).startsWith(label)) {
      // 名称已在后文：去掉命中本体（与其与名称间的冗余空白），避免同名重复铺开
      last = m.index + m[0].length + gap.length
      continue
    }
    out += label
    const gloss = PAREN_GLOSS_RE.exec(rest)
    // 括注与标签互为包含＝同一身份的两种写法，去重（无包含关系的新信息括注保留）
    last = m.index + m[0].length
      + (gloss && (label.includes(gloss[1]) || gloss[1].includes(label)) ? gloss[0].length : 0)
  }
  return out + text.slice(last)
}

/** 单段纯文本（已过保护面切分）的目录映射。 */
function readablePlainSegment(seg: string, scope: TraceScope): string {
  let out = replaceWithCatalogLabel(seg, FREE_ID_RE, (id) => {
    const p = scope.products.find((x) => x.product_id === id)
    if (!p) return null
    const label = p.scope_label || p.product_name
    if (!label) return null
    // 同名商品且标签未带品牌（消歧不足）：保留原文，不猜身份（productSubjectDisplay 同判定）
    const sameName = scope.products.filter((x) => x.product_name === p.product_name).length
    return sameName > 1 && label === p.product_name ? null : label
  }, scope, true)
  let shortMapped = ''
  let shortLast = 0
  FREE_SHORT_ID_RE.lastIndex = 0
  for (let m = FREE_SHORT_ID_RE.exec(out); m; m = FREE_SHORT_ID_RE.exec(out)) {
    shortMapped += out.slice(shortLast, m.index)
    const before = out.slice(Math.max(0, m.index - 12), m.index)
    const after = out.slice(m.index + m[0].length, m.index + m[0].length + 32)
    // A short suffix is an identity only in an explicit product/object/ID phrase, or in the
    // observed period-status grammar (054 基期记录冲突 / 055 跨期身份不一致). Nearby words such
    // as “订单” alone are insufficient: they also occur around ordinary numeric readouts.
    const explicitIdentity = /(?:商品|对象|编号|ID)\s*(?:为|是|[:：#])?\s*$/i.test(before)
    const periodStatusIdentity = /^\s*(?:的\s*)?(?:基期|对比期|跨期|本期)\s*(?:记录|字段|播放|订单|数据|身份|名称)?\s*(?:缺失|冲突|不一致|无记录)/.test(after)
    const candidates = explicitIdentity || periodStatusIdentity
      ? scope.products.filter((p) => p.product_id.endsWith(m![0]))
      : []
    const product = candidates.length === 1 ? candidates[0] : null
    const label = product?.scope_label || product?.product_name
    const sameName = product
      ? scope.products.filter((p) => p.product_name === product.product_name).length
      : 0
    shortMapped += label && !(sameName > 1 && label === product?.product_name) ? label : m[0]
    shortLast = m.index + m[0].length
  }
  out = shortMapped + out.slice(shortLast)
  out = replaceWithCatalogLabel(out, FREE_METRIC_RE, (code) => scope.metrics?.[code] ?? null, scope)
  out = out.replace(/\b(period_[A-Za-z0-9_]+)\b([，,]\s*\d{4}-\d{2}-\d{2}~(?:(?:\d{4})-)?\d{2}-\d{2})?/g,
    (_match, pid: string, trailingRange: string | undefined) => {
    const label = periodLabel(pid, scope)
    if (!label || label === pid) return pid + (trailingRange ?? '')
    const labelDates = /^(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})$/.exec(label)
    const duplicate = trailingRange
      ? /^[，,]\s*(\d{4}-\d{2}-\d{2})~(?:(\d{4})-)?(\d{2}-\d{2})$/.exec(trailingRange)
      : null
    const duplicateEnd = duplicate && `${duplicate[2] ?? duplicate[1].slice(0, 4)}-${duplicate[3]}`
    if (labelDates && duplicate && duplicate[1] === labelDates[1] && duplicateEnd === labelDates[2]) return label
    return label + (trailingRange ?? '')
  })
  out = out.replace(FREE_PUBLISHED_RE, '当期是否发布新增视频')
  out = out.replace(/unknown\(([^)]+)\)/g, (_token, state: string) => {
    return cohortReadingText(`unknown(${state})`)
  })
  return out
}

/** 自由文本 → 展示文本（scope 缺省＝原样返回，旧调用零影响；受保护段不做任何替换）。 */
export function readableFreeText(text: string, scope: TraceScope | null | undefined): string {
  const raw = String(text ?? '')
  if (!raw) return raw
  const catalog = scope ?? { products: [], period_choices: {}, periods: [], metrics: {} }
  return raw.split(PROTECT_SPLIT_RE)
    .map((seg, i) => (i % 2 === 1 ? seg : readablePlainSegment(seg, catalog)))
    .join('')
}
