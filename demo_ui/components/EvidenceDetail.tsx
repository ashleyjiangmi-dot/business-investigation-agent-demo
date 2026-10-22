import type { EvidenceDetail as Detail } from '@/lib/trace-filter'
import type { TraceScope } from '@/lib/trace'
// 使用相对路径以支持独立静态渲染。
import { describeEvidenceDetail } from '../lib/evidence-view'

/**
 * 展示服务端已登记证据的对象、日期、数值与来源。
 * 定位失败时仅说明暂不可核对，不生成数值；引用编号与完整映射放在技术追溯折叠区。
 */
export default function EvidenceDetail({
  detail,
  scope,
  onClose,
}: {
  detail: Detail
  scope: TraceScope
  onClose: () => void
}) {
  const view = describeEvidenceDetail(detail, scope)
  return (
    <div className={`evidence-detail${view.unavailable ? ' unavailable' : ''}`} role="region"
         aria-label="数据依据详情">
      <div className="ed-head">
        <h4>{view.title}</h4>
        <button type="button" className="toggle link ed-close" onClick={onClose} aria-label="收起数据依据">
          收起依据 ▴
        </button>
      </div>
      <dl className="ed-lines">
        {view.lines.map((l, i) => (
          <div className="ed-row" key={`${l.label}-${i}`}>
            <dt>{l.label}</dt>
            <dd>{l.value}</dd>
          </div>
        ))}
      </dl>
      {view.boundary && <p className="ed-boundary">{view.boundary}</p>}
      <details className="ed-tech">
        <summary>原始引用</summary>
        <p className="ed-tech-map">{view.techTrace}</p>
        <code>{view.rawRef}</code>
      </details>
    </div>
  )
}
