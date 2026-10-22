

export type RunOutcomeStatus = '需复核' | '未生成业务回答' | '完成'

export type RunOutcome = { status: RunOutcomeStatus; notice: string }

/** 交付降级终态 → 用户可读告警（与既有页面文案逐字一致）。 */
const DELIVERY_WARNINGS: Record<string, string> = {
  truncated_degraded: '本次回答在达到轮次上限时截断；以下内容不是正常完成的终答。',
  evidence_unverified: '本次交付未通过证据核验；以下内容不应视为已核验答案。',
  truncated_evidence_unverified: '本次回答已截断且未通过证据核验；以下内容不应视为正常完成的已核验答案。',
}

/** 本轮运行已结束但没有可交付业务正文时的状态标签（服务端 `no_final_answer`）。 */
export const NO_BUSINESS_ANSWER_STATUS: RunOutcomeStatus = '未生成业务回答'

/**
 *  临时恢复异常：调查本身可能仍在后台运行，只是暂时取不回结果。
 * 与「调查实际失败」分开，避免把网络/服务端暂时不可达显示成调查失败。
 */
export const RESUME_TRANSIENT_STATUS = '暂未取回'

export const RESUME_TRANSIENT_NOTICE =
  '暂时取不回上次调查结果（服务端连接异常）；调查可能仍在后台进行。' +
  '可稍后点「重新尝试取回」，本次不会自动重新提交。'

/** 缺答提示正文（`no_final_answer` 状态；`run_finished.has_business_answer=false` 时同样使用）。 */
export const NO_BUSINESS_ANSWER_NOTICE =
  '本次运行已结束，但未生成业务回答：模型未产出可交付的业务正文，原始输出可在技术诊断中回查。'

/**
 * `result_status` → 状态标签与提示。返回 null＝该取值不由本函数解释（调用方保持既有行为）。
 */
export function runOutcome(resultStatus: string): RunOutcome | null {
  const warning = DELIVERY_WARNINGS[resultStatus]
  if (warning) return { status: '需复核', notice: warning }
  if (resultStatus === 'no_final_answer') {
    return { status: NO_BUSINESS_ANSWER_STATUS, notice: NO_BUSINESS_ANSWER_NOTICE }
  }
  if (resultStatus === 'has_final_answer') return { status: '完成', notice: '' }
  return null
}


export function mergeRunOutcome(base: RunOutcome, hasBusinessAnswer: boolean | null | undefined): RunOutcome {
  if (hasBusinessAnswer !== false) return base
  return { status: base.status, notice: `${base.notice} ${NO_BUSINESS_ANSWER_NOTICE}` }
}

/* ==================== 局部证据问题的用户可见分层 ==================== */


/** 证据降级终态（`result_status` 取值；与 DELIVERY_WARNINGS 的键同源）。 */
const EVIDENCE_DEGRADED_STATES = new Set([
  'evidence_unverified',
  'truncated_evidence_unverified',
])

/** 正文隐藏引用占位（服务端 `_redact_citations` 的统一落点；此处只读镜像，供诊断/测试核对）。 */
export const HIDDEN_REF_PLACEHOLDER = '〔无效引用，已隐藏〕'

/** 局部引用提示：只说已知受损处，不推断整段结论不可信、也不承诺未标注内容一定有效。 */
function localEvidenceNotice(hidden: number, truncated: boolean): string {
  const parts: string[] = []
  if (truncated) parts.push('本次回答在达到轮次上限时截断；')
  parts.push(
    `本次回答有 ${hidden} 处引用未通过核验，已在正文对应位置标出；` +
    '引用核验只针对引用本身，业务数据仍以实际查询结果为准。',
  )
  return parts.join('')
}

/**
 * 证据降级终态的用户提示分层：可定位到具体受损引用时改述为「逐处标出」，
 * 并保留 `mergeRunOutcome` 已合并的缺答事实（不改状态标签）；不可定位时原样返回。
 * 调用方传入服务端原始 `result_status`（分层只认证据降级两个取值，不猜别的降级原因）。
 */
export function localEvidenceOutcome(
  base: RunOutcome,
  resultStatus: string,
  hiddenInAnswer: number | null | undefined,
): RunOutcome {
  if (!EVIDENCE_DEGRADED_STATES.has(resultStatus)) return base
  const hidden = typeof hiddenInAnswer === 'number' && Number.isFinite(hiddenInAnswer)
    ? Math.trunc(hiddenInAnswer)
    : null
  if (hidden === null || hidden <= 0) return base
  const notice = localEvidenceNotice(
    hidden, resultStatus === 'truncated_evidence_unverified')
  const noAnswer = base.notice.includes(NO_BUSINESS_ANSWER_NOTICE)
    ? ` ${NO_BUSINESS_ANSWER_NOTICE}`
    : ''
  return { status: base.status, notice: notice + noAnswer }
}
