import type { InvestigationScopeView } from '../lib/display-blocks'

/**
 * 结果区轻量调查范围摘要：只列本次实际调查对象、实际分析周期，以及会改变业务解读的
 * 指标范围/比较基准/筛选范围差异。值全部来自 `buildInvestigationScopeView` 的确定性映射
 * （实际查询投影＋已核验事实周期），不铺可查询范围、成功查询次数或证据表格。
 */
export default function InvestigationScope({ view }: { view: InvestigationScopeView }) {
  return (
    <dl className="investigation-scope" aria-label="本次调查范围">
      {view.rows.map((row) => (
        <div className="investigation-scope-row" key={row.key}>
          <dt>{row.label}</dt>
          <dd>{row.text}</dd>
        </div>
      ))}
    </dl>
  )
}
