import type { KnowledgeSource } from './trace-filter'

/**
 * 知识点（知识文档）引用的展示索引：本 run 实际检索到的 `doc_id → {标签, 标题}`。
 *
 * 纪律：
 * - 只认本轮 `knowledge_sources`（服务端登记的实际检索结果）；未登记文档**不建条目**，
 *   调用方退纯文本，不伪造来源、不生成可点入口；
 * - 编号按同族（指南/案例）**首次检索返回顺序**从 1 起，同一文档全文同一编号；
 * - 与 `[E]` 数值证据是两套体系：本索引不参与 Evidence Guard / 核验，只服务展示与来源卡片。
 */
export type KnowledgeRef = {
  docId: string
  kind: 'guide' | 'case'
  /** 同族内稳定序号（1 起） */
  n: number
  /** 轻量展示标签：`指南 1` / `案例 1` */
  label: string
  title: string
}

const KIND_LABEL: Record<'guide' | 'case', string> = { guide: '指南', case: '案例' }

/** 文档族：优先服务端 `doc_type`，缺失时按 doc_id 前缀判定（GUIDE/CASE），都不符=null（不猜）。 */
export function knowledgeKindOf(source: { doc_id: string; doc_type: string }): 'guide' | 'case' | null {
  if (source.doc_type === 'guide' || source.doc_type === 'case') return source.doc_type
  if (/^GUIDE\d+$/i.test(source.doc_id)) return 'guide'
  if (/^CASE\d+$/i.test(source.doc_id)) return 'case'
  return null
}

/** 本轮知识来源 → doc_id 索引（保序、同族分别编号、同文档去重）。 */
export function knowledgeRefIndex(
  sources: KnowledgeSource[] | null | undefined,
): Map<string, KnowledgeRef> {
  const map = new Map<string, KnowledgeRef>()
  const counters: Record<'guide' | 'case', number> = { guide: 0, case: 0 }
  for (const source of sources ?? []) {
    const docId = String(source?.doc_id ?? '').trim()
    if (!docId || map.has(docId)) continue
    const kind = knowledgeKindOf(source)
    if (!kind) continue
    counters[kind] += 1
    map.set(docId, {
      docId,
      kind,
      n: counters[kind],
      label: `${KIND_LABEL[kind]} ${counters[kind]}`,
      title: String(source.title ?? '').trim(),
    })
  }
  return map
}

/** 未登记/无可点入口时的纯文本标签（旧泛化说法，非可点入口，不伪造来源）。 */
export function knowledgeFallbackLabel(token: string): string {
  if (/^GUIDE\d+$/i.test(token)) return '经营指南'
  if (/^CASE\d+$/i.test(token)) return '经营案例'
  return token
}
