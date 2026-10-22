import type { Step, StepState } from '@/lib/trace'

const MARK: Record<StepState, string> = { planned: '·', running: '○', done: '✓', failed: '!' }
// `detail` 是服务端下发的具体拒绝码（step_finished.result_code）。只登记主查询链上真实可达、
// 且有确定业务含义的码；未登记的码走中性兜底（「服务未返回可用结果」），不猜测、不一律归因成周期。
const FAILURE_LABELS: Record<string, string> = {
  aggregate_metric_not_supported: '该指标不支持求和',
  budget_reached: '已达本次查询上限',
  knowledge_duplicate_query: '重复检索已跳过',
  knowledge_invalid_params: '检索参数未通过校验',
  knowledge_search_closed: '本次检索已停止',
  knowledge_unavailable: '经营知识暂不可用',
  period_rejected: '周期范围未通过校验',
  root_period_scope_invalid: '周期范围未通过校验',
  time_out_of_allowed_scope: '该周期不在可查范围内',
  time_semantics_invalid: '周期范围未通过校验',
  tool_http_error: '查询服务暂不可用',
  tool_invalid_args: '查询参数未通过校验',
  unknown_period_id: '周期标识不存在',
  unknown_tool: '工具不在当前可用范围',
}

function displayText(text: string) {
  return text
    .replace(/\bM\d+\b(?:\s*\/\s*\bM\d+\b)*/g, '')
    .replace(/\s*·\s*·\s*/g, ' · ')
    .replace(/^\s*·\s*|\s*·\s*$/g, '')
    .replace(/[ \t]{2,}/g, ' ')
    .trim()
}

function phrase(s: Step): string {
  const text = displayText(s.text)
  if (s.state === 'planned') return `准备${text}…`
  if (s.state === 'running') return `正在${text}…`
  if (s.state === 'failed') {
    const reason = s.detail ? FAILURE_LABELS[s.detail] ?? '服务未返回可用结果' : ''
    return `${text}未完成${reason ? `（${reason}）` : ''}`
  }
  return `已${text}`
}

export type CollapsedStep = { step: Step; repeat: number }

/**
 * 展示层合并：**连续、已完成且可见文案相同**的步骤合成一条，并标注连续次数
 * （例如连续两次「已检索经营方法」）。只影响 `TraceSteps` 的呈现——原始事件、步骤数量与
 * 实际调用记录不修改；不同工具/不同结果（failed 的具体原因不同）不合并，运行中/计划中
 * 也不合并（过程仍在生长，合并会让实时反馈失真）。
 */
export function collapseRepeatedSteps(steps: Step[]): CollapsedStep[] {
  const out: CollapsedStep[] = []
  for (const step of steps) {
    const last = out[out.length - 1]
    if (last
      && last.step.state === 'done' && step.state === 'done'
      && last.step.tool === step.tool
      && (last.step.detail ?? '') === (step.detail ?? '')
      && last.step.text === step.text
      && phrase(last.step) === phrase(step)) {
      last.repeat += 1
      continue
    }
    out.push({ step, repeat: 1 })
  }
  return out
}

/** 调查过程折叠标题的展示条目数（与实际事件数区分；实际调用记录不被改写）。 */
export function collapsedStepCount(steps: Step[]): number {
  return collapseRepeatedSteps(steps).length
}

export default function TraceSteps({ steps }: { steps: Step[] }) {
  return (
    <ol className="steps">
      {collapseRepeatedSteps(steps).map(({ step: s, repeat }) => (
        <li key={s.key} className={s.state}>
          <span className="mark">{MARK[s.state]}</span>
          <span className="text">
            {phrase(s)}
            {repeat > 1 && <span className="step-repeat">（连续 {repeat} 次）</span>}
          </span>
        </li>
      ))}
    </ol>
  )
}
