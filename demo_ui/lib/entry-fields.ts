import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { periodsInTimeOrder, type TraceScope } from './trace'

export type EntryField = {
  name: string
  label: string
  kind: 'paragraph' | 'select'
  required: boolean
  maxLength?: number
  options: string[]
}

/** 周期目录行（运行时下发； 的目标期/对比基准下拉与标签都从这里来，不写死周期映射） */
export type PeriodOption = {
  period_id: string
  period_label: string
  period_start_date: string
  period_end_date: string
  length_days?: number
}

export type EntryFields = {
  fields: EntryField[]
  /** 商品身份与周期标签的运行时投影：页面把工具参数里的人话映射需要它，不在前端另建目录 */
  scope: TraceScope
}

type StartVariable = {
  variable: string
  label: string
  type: string
  required?: boolean
  max_length?: number
  options?: string[]
  default?: string
}

type CtxResponse = {
  ok?: boolean
  entry_projection?: {
    all_label?: string
    period_choices?: Record<string, string[]>
    product_options?: string[]
    /** 品类快捷选项标签（服务端单点现算；旧 SSE/旧服务缺键＝无品类快捷方式，不影响全池/单商品） */
    category_options?: string[]
  }
  catalog?: {
    products?: {
      product_id: string
      product_name: string
      category: string
      /** 同名商品消歧标签由服务端单点现算（同名＝「名称（品牌）」），页面不另算口径 */
      brand?: string
      scope_label?: string
    }[]
    periods?: PeriodOption[]
  }
}

/** 从字段定义读取入口表单结构；选项、目录、周期和指标名称由运行时上下文补齐。 */
const ENTRY_FIELDS_PATH =
  process.env.OAGENT_GRAPH_PATH ||
  path.resolve(process.cwd(), '../core/data/entry_fields.json')
/** 入口上下文由 Agents SDK 服务层提供。 */
const SERVING_URL = process.env.OAGENT_SERVING_URL || 'http://127.0.0.1:8327'

type EntryFieldsDefinition = {
  fields?: StartVariable[]
  metric_labels?: Record<string, string>
}

async function readEntryFieldsDefinition(): Promise<EntryFieldsDefinition> {
  const text = await readFile(ENTRY_FIELDS_PATH, 'utf8')
  return JSON.parse(text) as EntryFieldsDefinition
}

/**
 * 输入项=入口字段定义（字段/标签/必填/场景枚举），商品与周期选项=服务端运行时下发。
 * 下发不可用时显式失败，不回退构建期副本（与入口适配器同一纪律）。
 */
export async function buildEntryFields(): Promise<EntryFields> {
  const [definition, ctx] = await Promise.all([
    readEntryFieldsDefinition(),
    fetchCtx(),
  ])
  const variables = definition.fields ?? []
  if (!variables.length) {
    throw new Error(`入口字段定义 ${ENTRY_FIELDS_PATH} 缺少 fields（core/data/entry_fields.json）`)
  }
  const proj = ctx.entry_projection
  const products = ctx.catalog?.products
  const periods = ctx.catalog?.periods
  if (!ctx.ok || !proj?.product_options?.length || !proj.period_choices || !products?.length
      || !periods?.length) {
    throw new Error('入口上下文响应缺少 entry_projection、catalog.products 或 catalog.periods（不回退图内副本）')
  }
  // 品类快捷选项＝服务端下发标签（旧服务缺键＝空数组，退回全池/单商品两类，行为不变）
  const categoryOptions = (proj.category_options ?? []).map(String)
  return {
    fields: variables.map((v) => ({
      name: v.variable,
      label: v.label,
      kind: v.type === 'paragraph' ? 'paragraph' : 'select',
      required: v.required !== false,
      maxLength: v.max_length,
      options: optionsFor(v, proj, periods, categoryOptions),
    })),
    scope: {
      products: products.map((p) => ({
        product_id: String(p.product_id),
        product_name: p.product_name,
        category: p.category,
        brand: p.brand,
        scope_label: p.scope_label,
      })),
      period_choices: proj.period_choices,
      periods,
      // 指标代号→中文名：入口字段定义的静态投影（单点现算自指标说明文本），
      // 页面不另维护第二套口径；取不到即空映射，证据行退化为只显示代号。
      metrics: definition.metric_labels ?? {},
    },
  }
}

async function fetchCtx(): Promise<CtxResponse> {
  // 模型和数据服务凭据只由服务端持有，此入口请求不携带凭据。
  const res = await fetch(`${SERVING_URL}/v1/oagent/ctx`)
  if (!res.ok) throw new Error(`取入口上下文失败 HTTP ${res.status}（serving ${SERVING_URL}）`)
  return (await res.json()) as CtxResponse
}

function optionsFor(
  v: StartVariable,
  proj: NonNullable<CtxResponse['entry_projection']>,
  periods: NonNullable<CtxResponse['catalog']>['periods'],
  categoryOptions: string[] = [],
): string[] {
  const products = proj.product_options ?? []
  // 两期下拉：选项=下发周期目录里的真实 period_id，时间序单点=lib/trace 的日期区间排序
  // （目录出现顺序与 ID 字符串序都不可信——目录尾部才是 p1/p2）。
  const chronological = periodsInTimeOrder(periods ?? []).map((p) => String(p.period_id))
  switch (v.variable) {
    // 商品/范围＝「全部商品 ／ 品类 · X ／ 单商品」三类共用同一个可搜索控件。
    // 品类标签与服务端成员映射同源（下发 category_options），前端不写死品类名、不另算成员。
    // 单商品仍保持目录原序（不按品类重排、不装成优先级）。
    case 'product_scope':
      return [proj.all_label as string, ...categoryOptions, ...products]
    // /经营周期＝可选时间上下文（首项「不指定」＝按问题里的日期/时段理解，
    // 由入口按  模拟业务日期解释）；对比基准＝可选项，空值＝不设显式基准。
    case 'target_period_id':
      return ['', ...chronological]
    // 对比基准＝可选项；空值＝不设显式基准，由入口自动取目标期的相邻上一可比期。
    // 页面在提交前删掉空值，入口合同只收真实 period_id 或不收该键。
    case 'base_period_id':
      return ['', ...chronological]
    default:
      return v.options ?? []
  }
}
