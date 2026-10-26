'use client'

import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import type { EntryField } from '@/lib/entry-fields'
import { takeFrames, type DifyEvent } from '@/lib/sse'
import {
  EMPTY_SCOPE,
  EMPTY_TRACE,
  applyTraceEvent,
  periodLabel,
  periodsInTimeOrder,
  type Trace,
  type TraceScope,
} from '@/lib/trace'
import type { ScopePeriod } from '@/lib/trace'
import {
  parseReport,
  type CompactReport,
} from '@/lib/report'
import { buildStructuredOutcome, type StructuredOutcome } from '@/lib/structured-report'
import { mergeRunOutcome, runOutcome, localEvidenceOutcome, RESUME_TRANSIENT_STATUS, RESUME_TRANSIENT_NOTICE } from '@/lib/run-status'
import {
  clearRunSession,
  loadRunSession,
  newRunId,
  sameInputs,
  saveRunSession,
  type SessionStore,
} from '@/lib/run-session'
import {
  buildBlockViews,
  buildInvestigationScopeView,
  collectActualPeriods,
  usableDisplayBlocks,
  type ActualPeriodRef,
} from '@/lib/display-blocks'
import { citeNumberMap, citeSummary, evidenceByHandle } from '@/lib/evidence-view'
import { knowledgeRefIndex } from '@/lib/knowledge-sources'
import type { DisplayBlocks, EvidenceDetail, KnowledgeSource, QueryScope } from '@/lib/trace-filter'
import type { CiteRender } from '@/components/MarkdownLite'
import TraceSteps, { collapsedStepCount } from '@/components/TraceSteps'
import ResultCard from '@/components/ResultCard'
import DisplayBlocksView from '@/components/DisplayBlocks'
import EvidenceDetailPanel from '@/components/EvidenceDetail'
import KnowledgeSourceCard from '@/components/KnowledgeSourceCard'
import InvestigationScope from '@/components/InvestigationScope'
import Combobox from '@/components/Combobox'

const FAILURE = '运行失败，请重试'
/** 商品目录类字段：选项多，用「输入过滤 + 下拉选择」；周期类项少，用同一样式的只读选择。 */
const FILTERABLE_FIELDS = new Set(['product_scope'])
/**
 *  周期字段：`target_period_id`＝经营目标期（常驻，本轮结论落在哪一期），
 * `base_period_id`＝可选的显式对比基准（空值＝不设基准，由入口自动取目标期的相邻上一
 * 可比期）。两者都不再是「本期/上期/两期对比」那种互斥查询权限模式；提交值仍是目录内
 * period_id，展示标签用目录日期现算。
 */
const PERIOD_FIELDS = new Set(['target_period_id', 'base_period_id'])
/** 页面侧可选项（标签带「可选」角标）：对比基准与经营周期。 */
const OPTIONAL_MARK_FIELDS = new Set(['base_period_id', 'target_period_id'])
const FIELD_ORDER = ['user_question', 'product_scope', 'target_period_id', 'base_period_id']
const FIELD_LABELS: Record<string, string> = {
  user_question: '经营问题',
  product_scope: '商品 / 品类 / 范围',
  target_period_id: '经营周期',
  base_period_id: '对比基准',
}

function orderedFields(fields: EntryField[]) {
  return [...fields].sort((a, b) => {
    const ai = FIELD_ORDER.indexOf(a.name)
    const bi = FIELD_ORDER.indexOf(b.name)
    return (ai < 0 ? FIELD_ORDER.length : ai) - (bi < 0 ? FIELD_ORDER.length : bi)
  })
}

function displayLabel(field: EntryField) {
  return FIELD_LABELS[field.name] ?? field.label
}

/** 周期下拉的展示文本＝精简日期区间（MM-DD~MM-DD，目录内全部同期年份，不重复展示；
 *  提交值仍是 period_id，数据为固定模拟周期，模拟口径由页面统一说明集中交代）。 */
function periodDateLabel(p: ScopePeriod) {
  return `${p.period_start_date.slice(5)}~${p.period_end_date.slice(5)}`
}

export default function Page() {
  const [fields, setFields] = useState<EntryField[]>([])
  const [values, setValues] = useState<Record<string, string>>({})
  const [scope, setScope] = useState<TraceScope>(EMPTY_SCOPE)
  const [status, setStatus] = useState('加载中')
  const [notice, setNotice] = useState('')
  const [reason, setReason] = useState('')
  const [trace, setTrace] = useState<Trace>(EMPTY_TRACE)
  const [expanded, setExpanded] = useState(false)
  const [stream, setStream] = useState('')
  const [report, setReport] = useState<CompactReport | null>(null)
  /** 结果合同（report v1）的消费结局；absent=按 answer_md 可读视图渲染 */
  const [structured, setStructured] = useState<StructuredOutcome>({ status: 'absent' })
  /** 本 run 已核验引用的原始依据详情（route.ts 深层守卫后）；null=旧流/回查不可得 */
  const [evidenceDetails, setEvidenceDetails] = useState<EvidenceDetail[] | null>(null)
  /** 知识来源：本 run 实际检索到的知识文档（route.ts 形状核验后）；null=无检索/旧 SSE */
  const [knowledgeSources, setKnowledgeSources] = useState<KnowledgeSource[] | null>(null)
  /** 类型化展示块（route.ts filterEvent 深层过滤后）；null=无意图/畸形/旧 SSE，退原链 */
  const [displayBlocks, setDisplayBlocks] = useState<DisplayBlocks | null>(null)
  /** 本轮实际调查范围与比较基准（route.ts 深层守卫后）；null=旧 SSE 缺键，不渲染范围摘要。
   *  只消费实际调查投影；入口目录不代表实际调查对象。 */
  const [queryScope, setQueryScope] = useState<QueryScope | null>(null)
  /** 完成后输入区收纳为摘要（保留原问题与范围），底部「发起新调查 →」展开完整表单 */
  const [inputsMinimized, setInputsMinimized] = useState(false)
  /** 可选设置（经营周期 / 对比基准）默认收起；用户主动展开后保持展开。 */
  const [periodSettingsOpen, setPeriodSettingsOpen] = useState(false)
  const [lastInputs, setLastInputs] = useState<Record<string, string> | null>(null)
  /** 当前展开详情的 canonical 引用（null=未展开） */
  const [openEvidenceRef, setOpenEvidenceRef] = useState<string | null>(null)
  /** 当前展开的知识来源 doc_id（null=未展开；与数据依据详情互斥，同一时刻只开一张卡） */
  const [openKnowledgeDoc, setOpenKnowledgeDoc] = useState<string | null>(null)
  /** 「查看依据」详情区 DOM 定位锚点（正文/结构化数据行共用同一 openEvidenceRef） */
  const detailAnchorRef = useRef<HTMLDivElement | null>(null)

  /** 暂时取不回（非 404）时保留的可重试身份；null=无待重试的恢复。 */
  const [pendingResume, setPendingResume] = useState<{ runId: string; inputs: Record<string, string> } | null>(null)
  /** 同一挂载期内防并发重复提交（按钮 disabled 之外的第二道闸；跨刷新由服务端同 ID 去重兜底）。 */
  const activeRunRef = useRef<string | null>(null)
  /** `scope` 的实时镜像：恢复/重放发生在挂载后的异步回调里，不能读到 effect 创建时的旧 scope。 */
  const scopeRef = useRef<TraceScope>(EMPTY_SCOPE)
  /** 本次挂载是否已完成一次「刷新后恢复」尝试（避免与字段目录加载重复触发）。 */
  const restoreAttemptedRef = useRef(false)

  /** sessionStorage 只在浏览器可用；SSR/隐私模式下退回 null（不阻断调查，只是本次不可恢复）。 */
  function sessionStore(): SessionStore | null {
    try {
      return typeof window === 'undefined' ? null : window.sessionStorage
    } catch {
      return null
    }
  }

  /** 失败态只给一句「运行失败，请重试」+ 极简原因分类；细节只进 console 与服务端日志。 */
  function fail(why: string, detail?: unknown) {
    setStatus('失败')
    setExpanded(false)
    setNotice(FAILURE)
    setReason(why)
    console.error(why, detail)
  }

  useEffect(() => {
    void (async () => {
      // 刷新后使用原 run_id 只读恢复；优先加载目录以重建视图，目录失败不阻止恢复尝试。
      const attemptRestore = (defaults: Record<string, string> | null) => {
        if (restoreAttemptedRef.current) return
        restoreAttemptedRef.current = true
        const saved = loadRunSession(sessionStore())
        if (!saved) return
        if (defaults) {
          const restored: Record<string, string> = Object.fromEntries(
            Object.entries(defaults).map(([name, value]) => [name, saved.inputs[name] ?? value]),
          )
          if (saved.question) restored.user_question = saved.question
          setValues(restored)
          // 恢复的这次调查若带显式周期/基准，可选设置保持展开，让已生效条件可见。
          if ((restored.target_period_id ?? '') || (restored.base_period_id ?? '')) {
            setPeriodSettingsOpen(true)
          }
        }
        void resumeExisting(saved.runId, saved.inputs, '刷新后恢复同一运行')
      }

      const res = await fetch('/api/fields')
      const data = await res.json()
      if (!res.ok) {
        fail('字段目录未就绪', data.error)
        attemptRestore(null)
        return
      }
      setFields(data.fields)
      const defaults: Record<string, string> = Object.fromEntries(
        (data.fields as EntryField[]).map((f) => [f.name, f.kind === 'select' ? f.options[0] : '']),
      )
      // /经营周期是**可选时间上下文**，默认不预选任何周期——直接问「昨天哪些商品下滑」
      // 时不因表单已锁死某一周而改变题面含义；用户显式选择时才覆盖默认（ 模拟业务日期）。
      // 对比基准默认＝自动（空值，由入口取目标期的相邻上一可比期）。
      defaults.target_period_id = ''
      defaults.base_period_id = ''
      setValues(defaults)
      setScope({ ...EMPTY_SCOPE, ...data.scope })
      scopeRef.current = { ...EMPTY_SCOPE, ...data.scope }
      setStatus('等待')
      attemptRestore(defaults)
    })()
  }, [])

  const visibleFields = orderedFields(fields)
  const questionField = visibleFields.find((f) => f.name === 'user_question')
  const orderedPeriods = useMemo(() => periodsInTimeOrder(scope.periods ?? []), [scope.periods])
  // 首屏主视觉：商品/范围（比较对象已从界面移除，不再进这两组）。
  const scopeFields = visibleFields.filter((f) => f.name === 'product_scope')
  // 可选设置（默认收起）：经营周期 + 对比基准；沿用原控件，不做重新设计。
  const periodFields = visibleFields.filter((f) =>
    ['target_period_id', 'base_period_id'].includes(f.name),
  )
  // 可选设置（经营周期 / 对比基准）默认收起；用户主动展开后保持展开。
  const periodOrderIndexOf = (id: string) =>
    orderedPeriods.findIndex((p) => String(p.period_id) === id)
  /**
   * 周期下拉的展示标签：只用目录日期区间说话，不显示 period_demo_p2 这类内部 ID。
   * 目标期给「最新」定位；基准给「目标期的上一期」定位，空值＝自动（不设显式基准）。
   */
  function periodLabelFor(name: string, option: string): string {
    if (name === 'base_period_id' && option === '') return '自动（取目标期的上一可比期）'
    if (name === 'target_period_id' && option === '') return '不指定（按问题里的日期理解）'
    const i = periodOrderIndexOf(option)
    if (i < 0) return option
    const text = periodDateLabel(orderedPeriods[i])
    if (name === 'target_period_id') {
      return i === orderedPeriods.length - 1 ? `${text}（最新）` : text
    }
    return i === periodOrderIndexOf(values.target_period_id ?? '') - 1
      ? `${text}（目标期的上一期）` : text
  }
  function displayOption(field: EntryField, value: string): string {
    if (PERIOD_FIELDS.has(field.name)) return periodLabelFor(field.name, value)
    return value
  }
  /** 显式基准才校验先后；基准留空＝自动，由入口按目录时间序取目标期的上一可比期。 */
  const baseIsExplicit = (values.base_period_id ?? '') !== ''
  const periodPairValid = useMemo(() => {
    if (!baseIsExplicit) return true
    const bi = periodOrderIndexOf(values.base_period_id ?? '')
    const ti = periodOrderIndexOf(values.target_period_id ?? '')
    return bi >= 0 && ti >= 0 && bi < ti
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseIsExplicit, values.base_period_id, values.target_period_id, orderedPeriods])
  const canSubmit = fields.length > 0 && periodPairValid && fields.every((f) => {
    const value = values[f.name] ?? ''
    if (f.required && !value.trim()) return false
    if (f.kind === 'select' && value && !f.options.includes(value)) return false
    if (f.maxLength && value.length > f.maxLength) return false
    return true
  })

  /** 两期下拉里与另一侧冲突的选项置灰（禁止选到相同或倒序周期；合同侧仍会确定性复核）。 */
  function periodOptionDisabled(name: string, option: string): boolean {
    if (!PERIOD_FIELDS.has(name)) return false
    const i = periodOrderIndexOf(option)
    if (i < 0) return false
    const other = periodOrderIndexOf(name === 'base_period_id'
      ? (values.target_period_id ?? '') : (values.base_period_id ?? ''))
    if (other < 0) return false
    return name === 'base_period_id' ? i >= other : i <= other
  }

  function renderField(field: EntryField): ReactNode {
    const isQuestion = field.name === 'user_question'
    return (
      <label key={field.name} className={`field field--${field.name.replaceAll('_', '-')}${isQuestion ? ' field--question' : ''}`}>
        <span className="field-label">
          <span>{displayLabel(field)}</span>
          {OPTIONAL_MARK_FIELDS.has(field.name) && !field.required && (
            <span className="optional-mark">可选</span>
          )}
        </span>
        {field.kind === 'paragraph' ? (
          <textarea
            rows={5}
            maxLength={field.maxLength}
            required={field.required}
            placeholder="描述你想了解的经营问题"
            value={values[field.name] ?? ''}
            onChange={(e) => setValues({ ...values, [field.name]: e.target.value })}
          />
        ) : (
          <Combobox
            value={values[field.name] ?? ''}
            options={field.options}
            required={field.required}
            readOnly={!FILTERABLE_FIELDS.has(field.name)}
            placeholder={FILTERABLE_FIELDS.has(field.name)
              ? '输入关键词筛选，或选择全部商品 / 品类 / 单商品'
              : undefined}
            labelFor={(o) => displayOption(field, o)}
            isDisabled={PERIOD_FIELDS.has(field.name)
              ? (o) => periodOptionDisabled(field.name, o)
              : undefined}
            onChange={(v) => setValues({ ...values, [field.name]: v })}
          />
        )}
      </label>
    )
  }

  async function run() {
    if (!canSubmit) return
    // 已有一次提交/恢复在进行时不再发起——按钮 disabled 之外的第二道闸。极短时间内的
    // 重复触发若各生成一个 ID，服务端的同 ID 去重覆盖不到，这里先拦住。
    if (activeRunRef.current) return
    let res: Response
    // 对比基准留空＝不设显式基期（入口按目标期自动取相邻上一可比期），不提交空键；
    // 目标期常驻，随表单一起提交。
    const inputs: Record<string, string> = { ...values }
    // 未选周期时按  模拟业务日期（2026-05-27 为“昨天”）按题面日期/窗口解释；
    // 空键均不下发；空基准由入口自动取目标期相邻上一可比期。
    if (!inputs.target_period_id) delete inputs.target_period_id
    if (!inputs.base_period_id) delete inputs.base_period_id
    // 同一份输入在本会话已有运行 ⇒ 直接续看/取回**同一次运行**，不再 POST 重复执行
    // （服务端同 ID 同输入也只执行一次，这里是页面侧的第一道闸）。
    const existing = loadRunSession(sessionStore())
    if (existing && sameInputs(existing.inputs, inputs)) {
      await resumeExisting(existing.runId, inputs, '重新取回本次调查结果')
      return
    }
    // 明确的另一次调查：生成新 ID，把它与原问题、结构化输入一起存进本会话（刷新后仍可读）。
    const runId = newRunId()
    saveRunSession(sessionStore(), { runId, inputs, question: inputs.user_question ?? '' })
    setPendingResume(null)
    activeRunRef.current = runId
    setLastInputs(inputs)
    beginRun()
    try {
      res = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ inputs, run_id: runId }),
      })
    } catch (e) {
      activeRunRef.current = null
      fail('无法连接服务端', e)
      return
    }
    if (!res.ok || !res.body) {
      const detail = await res.text()
      activeRunRef.current = null
      // 同 ID 输入冲突＝确定性拒绝（同一次调查不应被换成另一份输入）；提示后由用户显式重开。
      if (res.status === 409) {
        fail('本次调查标识已被另一次输入占用', detail.slice(0, 2000))
        return
      }
      fail('服务端拒绝请求', detail.slice(0, 2000))
      return
    }
    await consumeStream(res)
    activeRunRef.current = null
  }

  /** 重置结果区为「运行中」的干净初态（新一轮调查与恢复同 ID 都从这里开始）。 */
  function beginRun() {
    setStatus('运行中')
    setNotice('')
    setReason('')
    setTrace(EMPTY_TRACE)
    setExpanded(false)
    setStream('')
    setReport(null)
    setStructured({ status: 'absent' })
    setEvidenceDetails(null)
    setDisplayBlocks(null)
    setQueryScope(null)
    setOpenEvidenceRef(null)
    setKnowledgeSources(null)
    setOpenKnowledgeDoc(null)
  }

  /** 消费一次 run 的事件流（首轮 POST 与只读恢复共用同一读法，收尾判定一致）。 */
  async function consumeStream(res: Response) {
    // 流结束标志使用本次调用的局部变量，避免异步回调读取创建时的旧 state。
    let receivedFinished = false
    let failed = false
    const streamFiles = res.headers.get('X-Stream-Files')
    const reader = res.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let sawStartedStep = false
    for (;;) {
      let next: ReadableStreamReadResult<Uint8Array>
      try {
        next = await reader.read()
      } catch (e) {
        if (!receivedFinished) fail('事件流读取中断', e)
        return
      }
      const { done, value } = next
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const { frames, rest } = takeFrames(buffer)
      buffer = rest
      for (const ev of frames) {
        if (ev.event === 'step_started' && !sawStartedStep) {
          sawStartedStep = true
          setExpanded(true)
        }
        setTrace((prev) => applyTraceEvent(prev, ev, scopeRef.current))
        if (ev.event === 'answer_delta') {
          const text = (ev.data as { text?: string } | undefined)?.text
          if (typeof text === 'string') setStream((prev) => prev + text)
        }
        // 「收到过 run_finished」与「该 run 成功」是两件事：终态 failed 也是正常收尾
        if (ev.event === 'run_finished') {
          receivedFinished = true
          conclude(ev)
        }
        if (ev.event === 'error') {
          failed = true
          const rc = (ev.data as { reason_class?: string } | undefined)?.reason_class
          fail(`服务返回错误（${rc ?? 'unknown'}）`, JSON.stringify(ev.data ?? ev).slice(0, 2000))
        }
      }
    }
    if (!receivedFinished && !failed) {
      fail('事件流未收到终态')
    }
    if (streamFiles) console.info(`本次事件流已落盘：${streamFiles}`)
  }

  /**
   *  只读恢复：用同一 `run_id` 走 GET 取回同一次运行的已有状态/终态。
   * 绝不在这里 POST（未知 ID＝服务重启或记录过期时，明确提示不可恢复；是否重开由用户决定）。
   *
   * 失败分两类，不能混为一谈：
   * - **404（记录确实没有）**该 ID 已被证明不可恢复 ⇒ 清掉会话里的活动运行，要求用户显式重开；
   * - **其余（502/5xx/网络异常）**只是**暂时**取不回，服务端可能仍在跑着这次运行 ⇒ 保留 ID 与
   *   会话记录，提示可稍后重试，绝不自动重新提交（清 ID 会让用户丢掉本可取回的结果）。
   */
  async function resumeExisting(runId: string, inputs: Record<string, string>, why: string) {
    if (activeRunRef.current) return            // 已有一次恢复/提交在跑，不叠加
    activeRunRef.current = runId
    setLastInputs(inputs)
    beginRun()
    setPendingResume(null)
    console.info(`[IX02] ${why}：run_id=${runId}（只读恢复，不重新执行）`)
    let res: Response
    try {
      res = await fetch(`/api/run?run_id=${encodeURIComponent(runId)}`, { method: 'GET' })
    } catch (e) {
      activeRunRef.current = null
      markResumeTransient(runId, inputs, '无法连接服务端', e)
      return
    }
    if (!res.ok || !res.body) {
      const detail = await res.text()
      activeRunRef.current = null
      console.warn('[IX02] 恢复失败', res.status, detail.slice(0, 500))
      if (res.status === 404) {
        // 记录确实不存在（服务已重启，或该运行已被淘汰）：明确不可恢复，清掉活动运行；
        // 新 ID 只在用户**显式重新提交**时生成（不在这里静默换 ID 重跑）。
        setStatus('失败')
        setExpanded(false)
        setNotice('无法恢复上次调查结果：服务可能已重启，或该运行记录已超出本地保留范围。请重新提交一次调查。')
        setReason('运行记录不可恢复（未知或已过期）')
        clearRunSession(sessionStore())
        setInputsMinimized(false)
        return
      }
      // 暂时性失败：ID 仍可能有效（后台任务可能还在跑），保留会话记录与活动 ID。
      markResumeTransient(runId, inputs, `恢复请求失败（HTTP ${res.status}）`, detail.slice(0, 500))
      return
    }
    await consumeStream(res)
    activeRunRef.current = null
    setPendingResume(null)
  }

  /** 暂时取不回：保留可重试入口，绝不自动重新提交。
   *  这不是「调查失败」：后台运行可能仍在继续，状态与提示按临时恢复异常呈现。 */
  function markResumeTransient(runId: string, inputs: Record<string, string>, reason: string, detail?: unknown) {
    setStatus(RESUME_TRANSIENT_STATUS)
    setExpanded(false)
    setNotice(RESUME_TRANSIENT_NOTICE)
    setReason('')
    setInputsMinimized(true)
    setPendingResume({ runId, inputs })
    console.warn('[IX02] 恢复暂时失败，保留活动运行 ID', runId, reason, detail)
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void run()
  }

  /**
   * 结果页唯一的「发起新调查」入口（原「修改问题，重新调查」＋「重新开始」合并而来）：
   * 只把输入区从摘要还原为可编辑表单——上一轮提交的输入仍在 `values` 里（经营问题、商品范围、
   * 周期/基准），用户可直接改，也可清空后重填。提交仍走 `run` 的既有流程：输入未变＝取回
   * 同一次运行；改过输入＝新 `run_id` 新调查。结果区保持对应上次提交（改动提示见 form-dirty-note）。
   */
  function startNewInvestigation() {
    setInputsMinimized(false)
    // 本次已提交条件带显式周期/基准时展开可选设置，保持可见。
    if ((values.target_period_id ?? '') || (values.base_period_id ?? '')) setPeriodSettingsOpen(true)
  }

  function conclude(ev: DifyEvent) {
    setExpanded(false)
    setInputsMinimized(true)   // 本轮完成后输入区收纳为摘要（保留原问题与范围）
    const data = (ev.data ?? {}) as Record<string, any>
    const succeeded = data.status === 'succeeded'
    const resultStatus = String(data.result_status ?? 'unknown')
    const md = typeof data.answer_md === 'string' ? data.answer_md : ''
    if (!succeeded || !md.trim()) {
      if (!succeeded) {
        fail(
          resultStatus === 'truncated_no_final_answer'
            ? '已达到模型轮次上限，未生成可展示的最终回答'
            : `上游返回失败终态（${resultStatus}）`,
          JSON.stringify(data).slice(0, 2000),
        )
      } else {
        fail('终答正文为空', JSON.stringify(data).slice(0, 500))
      }
      return
    }
    const parsed = parseReport(md)
    setReport(parsed)
    // 过滤后没有可展示数据块时使用完整正文，避免空壳投影占据结果区。
    const db = usableDisplayBlocks(
      (data.display_blocks ?? null) as DisplayBlocks | null, scopeRef.current)
    setDisplayBlocks(db)
    // 本轮实际调查范围投影（route.ts 已按服务端键面形状守卫；缺键/畸形=null）。
    setQueryScope((data.query_scope ?? null) as QueryScope | null)
    // 优先消费  合同（route.ts 已按白名单+深层形状守卫过滤，缺省/畸形=null）。
    // 有类型化展示块时抑制关键发现的三类卡片形状（同一结果不并排两套卡片），正文形状保留。
    //  §4：抑制只对**可用投影**生效——整份不可用时随投影一并恢复卡片形状（降级一次）。
    const outcome = buildStructuredOutcome(data.report ?? null, scopeRef.current, {
      suppressCardShapes: Boolean(db?.blocks?.length),
    })
    if (outcome.status === 'conflict') {
      // 结构与详情冲突＝UI 本地问题：回退完整详情，如实登记，不默默择优（卡片 §4 口径）。
      console.warn('[report] 结构化字段与 detail_md 不一致，已回退完整详情视图', outcome.mismatches)
    }
    setStructured(outcome)
    // 原始依据详情（旧 SSE 无此键/服务端回查不可得=null，退原有证据清单视图）。
    setEvidenceDetails(Array.isArray(data.evidence_details) ? data.evidence_details : null)
    // 知识来源：本 run 实际检索到的文档（旧 SSE 无此键=null，正文知识引用退纯文本）。
    setKnowledgeSources(Array.isArray(data.knowledge_sources) ? data.knowledge_sources : null)
    setOpenEvidenceRef(null)
    setOpenKnowledgeDoc(null)
    // 运行结束、交付降级与业务正文是否存在分别处理；占位文案不代表已生成业务回答。
    const hba = typeof data.has_business_answer === 'boolean' ? data.has_business_answer : null
    const baseOutcome = mergeRunOutcome(runOutcome(resultStatus) ?? { status: '完成', notice: '' }, hba)
    // 证据降级且能定位到具体受损引用（hidden_in_answer>0）时，覆盖式「整次未通过核验」
    // 提示改述为「逐处标出」；不再按「结论区是否含异常引用」追加主结论不可信判断。
    // 审计字段（result_status/hidden_in_answer）逐字不动。
    const hiddenInAnswer = typeof data.hidden_in_answer === 'number' ? data.hidden_in_answer : null
    const runState = localEvidenceOutcome(baseOutcome, resultStatus, hiddenInAnswer)
    setStatus(runState.status)
    setNotice(runState.notice)
  }

  const running = status === '运行中'
  const collapsed = !expanded
  const streamBody = stream

  /**
   *  轻量引用入口：本 run 有已登记详情时，正文/组件里的 [E#] 渲染为**轻量编号** `[1]`
   * （字号略小、浅灰、无底色与边框，不用长按钮；编号紧跟被引用的结论或读数）——
   * 多个引用分别成入口、分别查看；同一时刻展开一条，data-ref 定位原引用对应项。
   * - resolved → `[n]`（n＝本次已登记依据的稳定序号，同一证据各处编号一致；hover/title 给来源摘要）；
   * - 已登记但本次未能回查定位（missing）→ 「依据暂不可核对」，仍可点开看服务端原因（不给编号）；
   * - 完全未登记的引用（旧流未核验/正文多余短号）→ 非可点击的状态标记，不伪造有效依据入口；
   * - 无详情数据（旧 SSE/知识类）→ cite 缺省，正文逐字保留  纯文本形态。
   * `[[关键事实]][E#]` 走同一 handle→detail 查找，第二参数只替换可见载体；
   * 未登记句柄→非点击文字，missing→可点开原因的不可核对文字，不自动附着到周围文字。
   */
  const detailMap = useMemo(() => evidenceByHandle(evidenceDetails), [evidenceDetails])
  /**  轻量引用编号（单点＝`lib/evidence-view.citeNumberMap`，证据面板共用同一规则）。 */
  const citeNumber = useMemo(() => citeNumberMap(evidenceDetails), [evidenceDetails])
  /** 知识来源索引（doc_id → 「指南 n / 案例 n」+ 标题）：只认本轮实际检索到的文档。 */
  const knowledgeMap = useMemo(() => knowledgeRefIndex(knowledgeSources), [knowledgeSources])
  const activeDetail = openEvidenceRef ? detailMap.get(openEvidenceRef) ?? null : null
  const activeKnowledge = openKnowledgeDoc ? knowledgeMap.get(openKnowledgeDoc) ?? null : null
  // 详情展开（含清单里切换另一条）后滚入视口：详情区在结果区下方，不定位用户找不到。
  // 收起（openEvidenceRef/openKnowledgeDoc→null）与新一轮调查清空不触发滚动；知识来源卡片
  // 复用同一锚点与规则（两类卡片互斥，同一时刻只渲染一张）；系统减少动态效果时用非动画滚动；
  // 焦点移到锚点（preventScroll 避免二次滚动），键盘用户也能感知位置变化。
  useEffect(() => {
    const anchorActive = (openEvidenceRef && activeDetail) || (openKnowledgeDoc && activeKnowledge)
    if (!anchorActive) return
    const el = detailAnchorRef.current
    if (!el) return
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
    el.focus({ preventScroll: true })
  }, [openEvidenceRef, activeDetail, openKnowledgeDoc, activeKnowledge])
  const cite = useMemo<CiteRender | undefined>(() => {
    if ((!evidenceDetails || evidenceDetails.length === 0) && knowledgeMap.size === 0) return undefined
    return (handle: string, text?: string) => {
      // 知识文档引用（`[GUIDE01]`/`[CASE04]`）：本 run 实际检索到的文档 → 「指南 n / 案例 n」
      // 可点标签（打开知识来源卡片）；未登记 → null，由渲染层退纯文本，不给假入口。
      if (/^(GUIDE|CASE)\d+$/.test(handle)) {
        const k = knowledgeMap.get(handle)
        if (!k) return null
        const open = openKnowledgeDoc === k.docId
        return (
          <button
            type="button"
            className={`cite-num${open ? ' active' : ''}`}
            data-doc-id={k.docId}
            onClick={() => {
              setOpenEvidenceRef(null)
              setOpenKnowledgeDoc(open ? null : k.docId)
            }}
            aria-expanded={open}
            aria-label={`查看知识来源 ${k.label}${k.title ? ` · ${k.title}` : ''}`}
            title={`查看知识来源 · ${k.label}${k.title ? `（${k.title}）` : ''}`}
          >
            [{k.label}]
          </button>
        )
      }
      const d = detailMap.get(handle)
      if (!d) {
        // 无本 run 依据详情（旧 SSE/知识类答复）：保持  逐字，不伪造状态
        if (!evidenceDetails || evidenceDetails.length === 0) return text ? null : `[${handle}]`
        if (text) {
          // 文字锚点指向未登记句柄：去壳阅读，非可点击状态标记，不映射到其它合法 detail
          return (
            <span className="cite-text unavailable" title="本次调查未登记该依据，无法回查原始数据">
              {text}
            </span>
          )
        }
        return (
          <span className="cite-plain unavailable" title="本次调查未登记该依据，无法回查原始数据">
            依据暂不可核对
          </span>
        )
      }
      const open = openEvidenceRef === d.ref
      const missing = d.locate === 'missing'
      const n = citeNumber.get(handle)
      if (text) {
        // 文字锚点沿用同一 handle→canonical ref 与滚动/关闭逻辑，只换可见载体。
        return (
          <button
            type="button"
            className={`cite-text${open ? ' active' : ''}${missing ? ' unavailable' : ''}`}
            data-ref={d.ref}
            onClick={() => {
              setOpenKnowledgeDoc(null)
              setOpenEvidenceRef(open ? null : d.ref)
            }}
            aria-expanded={open}
            aria-label={missing
              ? `${text}：依据暂不可核对，点击查看原因`
              : `${text}：查看数据依据 · ${citeSummary(d, scope)}`}
            title={missing ? '本次未能回查该依据的原始记录，点击查看原因' : `查看数据依据 · ${citeSummary(d, scope)}`}
          >
            {text}
          </button>
        )
      }
      return (
        <button
          type="button"
          className={`cite-num${open ? ' active' : ''}${missing ? ' unavailable' : ''}`}
          data-ref={d.ref}
          onClick={() => {
            setOpenKnowledgeDoc(null)
            setOpenEvidenceRef(open ? null : d.ref)
          }}
          aria-expanded={open}
          aria-label={missing
            ? '依据暂不可核对，点击查看原因'
            : `查看数据依据 [${n ?? '?'}] · ${citeSummary(d, scope)}`}
          title={missing ? '本次未能回查该依据的原始记录，点击查看原因' : `查看数据依据 · ${citeSummary(d, scope)}`}
        >
          {missing ? '依据暂不可核对' : `[${n ?? '?'}]`}
        </button>
      )
    }
  }, [evidenceDetails, detailMap, citeNumber, knowledgeMap, openEvidenceRef, openKnowledgeDoc, scope])
  // 块视图（纯确定性映射；无块=null → ResultCard 三分支与  前逐字一致）
  const blockViews = useMemo(() => buildBlockViews(displayBlocks, scope), [displayBlocks, scope])
  const typedNode = blockViews.length
    ? <DisplayBlocksView views={blockViews} cite={cite} scope={scope} />
    : null
  const hasDisplayBlocks = blockViews.length > 0
  /**
   * 引用详情登记的实际周期：真实起止日期优先；目录能解析出标签时用标签；都没有时保留原 ID。
   * `periodId` 保留为去重身份，供与展示块里的同周期（可能是裸 ID）归并。
   */
  const evidencePeriods = useMemo<ActualPeriodRef[]>(() => {
    const refs: ActualPeriodRef[] = []
    const add = (p: EvidenceDetail['period']) => {
      if (!p) return
      const id = typeof p.period_id === 'string' && p.period_id ? p.period_id : null
      const dates = p.start_date && p.end_date ? `${p.start_date}~${p.end_date}` : ''
      const resolved = id ? periodLabel(id, scope) : ''
      const text = resolved && resolved !== id ? resolved : (dates || resolved)
      if (text) refs.push({ periodId: id, text })
    }
    for (const detail of evidenceDetails ?? []) {
      add(detail.period)
      add(detail.base)
      add(detail.target)
    }
    return refs
  }, [evidenceDetails, scope])
  /**
   * 实际分析周期：**合并**已核验事实行与引用详情两处真实来源，按周期身份去重。
   * 只取其中一处会漏掉多次查询实际用过的其他周期；不使用入口可查询周期顶替。
   */
  const actualPeriods = useMemo(
    () => collectActualPeriods(blockViews, evidencePeriods),
    [blockViews, evidencePeriods],
  )
  /** 结果区轻量「调查范围」：实际对象/周期 + 有差异的指标范围/比较基准/筛选范围。 */
  const investigationScope = useMemo(
    () => buildInvestigationScopeView(queryScope, actualPeriods, scope),
    [queryScope, actualPeriods, scope],
  )
  /** 调查过程折叠标题按**展示层合并后**的条目数计数（连续重复步骤不重复计）。 */
  const traceStepCount = useMemo(() => collapsedStepCount(trace.steps), [trace.steps])
  /** 输入区摘要行：上次实际提交的非空字段，按表单顺序给「标签 值」（提交值合同，不重推）。
   *  比较对象已无输入控件（改由题面描述），不进摘要——摘要只反映用户真正设定过的条件
   *  （问题 / 商品范围 / 周期 / 基准）。 */
  const summaryItems = lastInputs
    ? orderedFields(fields)
        .filter((f) => (lastInputs[f.name] ?? '').trim())
        .map((f) => ({
          key: f.name,
          label: displayLabel(f),
          value: displayOption(f, lastInputs[f.name]),
        }))
    : []
  /**
   * 当前表单 vs 上次**已提交**的调查条件。结果区永远对应上次提交——在未重新提交前改动
   * 控件不能让历史结果看起来随之改变（如实提示两者不同，不在前端把结果重算/改述）。
   */
  const formInputs = (() => {
    const inputs: Record<string, string> = { ...values }
    // 未选周期时按  模拟业务日期（2026-05-27 为“昨天”）按题面日期/窗口解释；
    // 空键均不下发；空基准由入口自动取目标期相邻上一可比期。
    if (!inputs.target_period_id) delete inputs.target_period_id
    if (!inputs.base_period_id) delete inputs.base_period_id
    return inputs
  })()
  const formChangedSinceRun = Boolean(lastInputs) && !sameInputs(lastInputs!, formInputs)

  return (
    <main className="app-shell">
      <header className="page-header">
        <h1>经营调查</h1>
      </header>

      <div className="workbench">
        <section className="card entry-card" aria-labelledby="entry-title">
          <div className="card-heading">
            <h2 id="entry-title">调查输入</h2>
          </div>

          {inputsMinimized && !running && summaryItems.length > 0 ? (
            <>
              <div className="entry-summary">
                <div className="entry-summary-fields">
                  {summaryItems.map((it) => (
                    <span className="entry-summary-item" key={it.key}>
                      <span className="entry-summary-label">{it.label}</span>
                      <span className="entry-summary-value">{it.value}</span>
                    </span>
                  ))}
                </div>
              </div>
              {/* 卡片底部唯一入口：蓝色文字链接（无按钮容器）。点击回到输入页，上一轮经营问题与
                  商品范围仍在表单里，可直接改或清空后发起全新调查。 */}
              <button type="button" className="entry-edit" onClick={startNewInvestigation}>
                发起新调查 →
              </button>
            </>
          ) : (
            <form onSubmit={onSubmit}>
            {questionField && renderField(questionField)}

            <div className="scope-fields">
              {scopeFields.map(renderField)}
            </div>

            {/* C01/T03：周期与基准是可选时间上下文，收进默认收起的「可选设置」——
                首屏只突出「问什么、分析哪些商品」；展开后才出现两个周期选择器，沿用原控件。
                首屏默认两者皆空，故从收起态进入；恢复/再编辑带回显式周期或用户上次展开过时，
                由 periodSettingsOpen 保持展开（见 attemptRestore 与 startNewInvestigation）。 */}
            <details
              className="advanced-fields"
              open={periodSettingsOpen}
              onToggle={(e) => setPeriodSettingsOpen(e.currentTarget.open)}
            >
              <summary>可选设置（经营周期、对比基准）</summary>
              <div className="advanced-fields-body">
                <div className="period-fields">
                  {periodFields.map(renderField)}
                </div>
                {/* 简短提示：不选周期时按问题里的日期/时段理解；选定后默认与上一可比期比较。 */}
                <p className="period-pair-hint">
                  经营周期是可选的时间上下文：不选时按问题里写的日期或时段理解（例如「昨天」
                  「4 月比 3 月」）；选定后问「下滑 / 上升 / 比以前」默认与它的上一可比期比较。
                  周期为固定模拟数据。
                </p>
              </div>
            </details>
            {/* 周期对校验失败：提示放在收起区外，收起状态下也看得见（错误不藏进折叠区）。 */}
            {!periodPairValid && (
              <p className="period-pair-hint" role="status">
                对比基准要早于经营周期，请重新选择。
              </p>
            )}

            <div className="submit-row">
              <button className="primary-button" type="submit" disabled={running || !canSubmit}>
                {running ? '调查进行中…' : '提交并调查'}
              </button>
            </div>
            {/* 改动尚未提交时如实说明结果区仍对应上次条件（不把表单现值当作已生效）。 */}
            {formChangedSinceRun && !running && (
              <p className="period-pair-hint form-dirty-note" role="status">
                下方结果仍对应上次提交的调查条件；当前改动要在重新提交后才生效。
              </p>
            )}
          </form>
          )}
        </section>

        <section className="card result-card" aria-labelledby="result-title" aria-live="polite">
          <div className="card-heading result-heading">
            <h2 id="result-title">调查结果</h2>
            <span className={`status-pill status-${status === '等待' ? 'waiting' : status === '运行中' ? 'running' : status === '完成' ? 'complete' : status === '失败' ? 'failed' : 'review'}`}>
              {status}
            </span>
          </div>

          {/** 页面统一说明演示数据口径。 */}
          <p className="caliber-note" role="note">演示数据，仅用于模拟经营分析。</p>

          {notice && (
            <p className={`notice${status === RESUME_TRANSIENT_STATUS ? '' : ' error'}`}>
              {notice}
              {reason && <span className="reason">（{reason}）</span>}
            </p>
          )}

          {pendingResume && !running && (
            <button
              type="button"
              className="toggle link resume-retry"
              onClick={() => void resumeExisting(pendingResume.runId, pendingResume.inputs, '再次尝试取回')}
            >
              重新尝试取回
            </button>
          )}

          {/* 结构化字段与详情不一致时只登记开发日志（见 conclude），运营仍正常阅读完整回答；
              不展示内部展示合同异常提示。 */}

          {/* 证据异常/降级提示由 run-status.ts 的运行状态提示统一负责，此处不再另建一套。 */}

          {/* 实际调查范围：轻量文字，只列实际对象/周期与有差异的口径。 */}
          {report && investigationScope && <InvestigationScope view={investigationScope} />}

          {report ? (
            <ResultCard
              report={report}
              structured={structured}
              scope={scope}
              cite={cite}
              typed={typedNode}
              hasDisplayBlocks={hasDisplayBlocks}
            />
          ) : running && stream ? (
            <div className="streaming">
              <p className="pulse">正在整理回答</p>
              {/* 流式正文用与终态同一规范业务正文来源（投影信封不进业务区；
                  信封已开始但正文标记未到＝留空等下一帧，不中途倾倒审计包络）。 */}
              <pre className="stream-text">{streamBody}</pre>
            </div>
          ) : (
            !running && !notice && <p className="empty-result">提交问题后，调查结果会显示在这里。</p>
          )}

          {report && activeDetail && (
            <div ref={detailAnchorRef} tabIndex={-1} className="evidence-anchor">
              <EvidenceDetailPanel detail={activeDetail} scope={scope} onClose={() => setOpenEvidenceRef(null)} />
            </div>
          )}

          {activeKnowledge && (
            <div ref={detailAnchorRef} tabIndex={-1} className="evidence-anchor">
              <KnowledgeSourceCard source={activeKnowledge} onClose={() => setOpenKnowledgeDoc(null)} />
            </div>
          )}

          <div className="trace-section">
            <div className="trace-heading">
              <h3>调查过程</h3>
              {trace.steps.length > 0 && (
                <button className="toggle steps-toggle" type="button" onClick={() => setExpanded((v) => !v)}>
                  {traceStepCount} 步 {collapsed ? '展开' : '收起'}
                </button>
              )}
            </div>
            {trace.steps.length > 0 ? (
              !collapsed && <TraceSteps steps={trace.steps} />
            ) : (
              <p className="trace-empty">
                {running
                  ? '已提交，等待第一条实际调查步骤…'
                  : status === '完成' || status === '需复核' || status === '未生成业务回答'
                    ? '本次没有可展示的调查过程。'
                    : status === RESUME_TRANSIENT_STATUS
                      ? '调查可能仍在后台进行；暂时取回结果后可查看调查过程。'
                      : status === '失败'
                        ? '调查失败前没有记录调查过程。'
                        : '开始调查后，过程会显示在这里。'}
              </p>
            )}
          </div>
        </section>
      </div>
    </main>
  )
}
