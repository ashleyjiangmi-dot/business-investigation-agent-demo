import type { KnowledgeRef } from '../lib/knowledge-sources'

/**
 * 知识来源卡片：点击正文 `[指南 1]` 后在结果区下方轻量展开，复用数据依据详情的展开/关闭
 * 交互与视觉样式（同一 `evidence-detail` 结构）。
 *
 * 只展示三项：类型与编号（「知识来源 · 指南 1」）、真实文档标题、原始文档 ID。
 * **不展示全文、不截取原文、不做段落级定位**，也不新建完整文档详情页；知识引用不进
 * Evidence Guard，本卡片不承载任何数值核验语义。
 */
export default function KnowledgeSourceCard({
  source,
  onClose,
}: {
  source: KnowledgeRef
  onClose: () => void
}) {
  return (
    <div className="evidence-detail" role="region" aria-label="知识来源详情">
      <div className="ed-head">
        <h4>知识来源 · {source.label}</h4>
        <button type="button" className="toggle link ed-close" onClick={onClose} aria-label="收起知识来源">
          收起来源 ▴
        </button>
      </div>
      <dl className="ed-lines">
        <div className="ed-row">
          <dt>文档标题</dt>
          <dd>{source.title || '（本轮检索结果未提供标题）'}</dd>
        </div>
        <div className="ed-row">
          <dt>文档 ID</dt>
          <dd><code>{source.docId}</code></dd>
        </div>
      </dl>
    </div>
  )
}
