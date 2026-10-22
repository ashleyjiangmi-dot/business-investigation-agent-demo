# -*- coding: utf-8 -*-
"""经营调查 FastAPI/SSE 服务与运行登记。

负责 Agent 调用、RunRegistry 生命周期、事件交付与用量记账。
结果处理交给 result_assembly，Wire 投影交给 core.wire_contract。
只下发白名单事件、查询摘要与处理后的正文，不下发 reasoning 或工具响应原文。
模型、客户端、上下文来源与知识检索器支持注入；有状态替身按运行独立创建。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from core.entry_adapter import assemble_run_input
from core.tool_client import ToolServerClient
from core.usage_ledger import UsageLedger
# 事件白名单与下发字段由 core.wire_contract 维护。
from core.wire_contract import (_citations_wire,
                                            _result_status, _sse, _wire_step_events,
                                            build_report,
                                            has_business_answer, knowledge_sources_wire,
                                            query_scope_wire,
                                            report_with_answer_line)

from . import display_intent, model_provider, runtime
# 结果处理由 result_assembly 负责；在此重导出，保证调用方与注入点使用同一函数对象。
from .result_assembly import (FINAL_ANSWER_SOURCE,  # noqa: F401  重新导出给 server.FINAL_ANSWER_SOURCE 消费方
                              _merge_category_total_fallback,
                              finish_display_blocks, post_process,
                              project_evidence_details)

log = logging.getLogger('agents_oagent.server')

SERVING_NAME = 'agents_oagent'
WIRE_SCHEMA = 'lg_wire_v1'
RUN_ID_PREFIX = 'as-'
RUN_ERROR = 'run_error'

# 重复 ID 仅允许复用同输入运行；不同输入返回 409。
# 未知或已淘汰 ID 返回 404，不重建任务。完成记录按创建顺序淘汰，运行中记录保留。
RUN_ID_CONFLICT = 'run_id_conflict'   # 同 ID 不同输入：409，不执行、不覆盖原运行
RUN_UNKNOWN = 'run_unknown'           # GET 未知 ID（含服务重启后旧 ID）：404，不创建任务
RUN_RETENTION = 32
# 订阅者续看已运行帧的轮询间隔：游标式取帧，只在无新帧时等待。不用 Event/Condition 是为了
# 不把 loop 绑定的原语留在跨请求共享的记录上（同一 app 可能在测试里跨事件循环使用）。
RUN_TAIL_POLL_S = 0.01

SSE_HEADERS = {'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'}

# 正文先交付，展示块核验仅在最终帧等待；超时将 display_blocks 置空，不阻塞正文。
DISPLAY_VERIFY_TIMEOUT_S = 10.0

# per-run 登记件（本地 out/，gitignore；不进 SSE、不进 result_json 键集合）
RUN_LEDGER_PATH = Path(__file__).resolve().parent / 'out' / 'run_ledger.jsonl'
USAGE_LEDGER_PATH = Path(__file__).resolve().parent / 'out' / 'usage.jsonl'

CtxFetcher = Callable[[], dict]


def _input_fingerprint(inputs: dict) -> str:
    """结构化输入的确定性指纹（键序无关）：同一 run_id 只接受同一份输入。

    用 canonical JSON 本身（不哈希）——冲突时可直接比对差异，且无需额外依赖；
    值不可序列化时退回 repr（仍然确定性，只用于相等比较，不参与执行）。"""
    try:
        return json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    except (TypeError, ValueError):
        return repr(inputs)


class _RunRecord:
    """进程内单次运行的登记：已装配帧缓冲 + 完成标志 + 执行任务。

    帧缓冲里放的是**已下发形状的 SSE 帧**（`_sse(...)` 产物，含 run_finished 终态），
    订阅者（POST 复用 / GET 恢复）只按游标重放，不重算、不再装配第二份终态。"""

    def __init__(self, run_id: str, fingerprint: str) -> None:
        self.run_id = run_id
        self.fingerprint = fingerprint
        self.created_at = time.time()
        self.frames: list[str] = []
        self.finished = False
        self.task: asyncio.Task | None = None

    def push(self, frame: str) -> None:
        self.frames.append(frame)

    def finish(self) -> None:
        self.finished = True


class RunRegistry:
    """进程内 run 生命周期的唯一登记。

    保证的是**单进程存活期内**的稳定 ID 语义：同 ID 同输入只执行一次、并发重复提交复用同一
    执行、只读重放已装配终态。不落盘、不跨重启、不多实例。服务重启后旧 ID 查不到＝「未知」，由调用方明确提示无法恢复，不静默重跑。"""

    def __init__(self, retention: int = RUN_RETENTION) -> None:
        self._records: dict[str, _RunRecord] = {}
        self._retention = max(1, int(retention))

    def get(self, run_id: str) -> _RunRecord | None:
        return self._records.get(run_id)

    def create(self, run_id: str, fingerprint: str) -> _RunRecord:
        record = _RunRecord(run_id, fingerprint)
        self._records[run_id] = record
        self._evict()
        return record

    def drop(self, run_id: str) -> None:
        """执行体未能启动时收回登记（不留一条永不完成的记录）。"""
        record = self._records.get(run_id)
        if record is not None and not record.finished and not record.frames:
            self._records.pop(run_id, None)

    def finish(self, record: _RunRecord) -> None:
        """标记完成并按上限淘汰（淘汰只发生在有记录刚完成时，不打断任何在跑运行）。"""
        record.finish()
        self._evict()

    def _evict(self) -> None:
        finished = sorted((r for r in self._records.values() if r.finished),
                          key=lambda r: r.created_at)
        for old in finished[:max(0, len(finished) - self._retention)]:
            self._records.pop(old.run_id, None)


def _failed_frame(result_status: str) -> str:
    """入口或上下文失败终态，不包含报告、引用详情或展示块。"""
    return _sse({'event': 'run_finished',
                 'data': {'status': 'failed', 'answer_md': '',
                          'citations': [], 'result_status': result_status,
                          'hidden_in_answer': None, 'report': None,
                          'knowledge_sources': None,
                          'evidence_details': None, 'display_blocks': None}})


def run_wire_view(result: runtime.AgentRunResult, projection: dict) -> dict:
    """将运行结果装配为下发投影需要的 final 结构，保留截断状态供终态分类。"""
    return {'status': result.status, 'error': result.error,
            'truncated': result.truncated,
            'budget_final': result.run_context.budget_final(),
            'projection_md': (projection or {}).get('projection_md', ''),
            'truncation': result.run_context.truncation}


def _ledger_entry(run_id: str, result: runtime.AgentRunResult, view: dict,
                  projection: dict, elapsed_ms: int) -> dict:
    """per-run 登记一行：run 标识、终态、模型请求数、预算对账、引用核验读数、步骤与 usage。

    读向与 的分支登记件同源（取已算出值，缺失按 null 如实登记、不代 0）；本链没有
    任何一轮「累积原文」，故 `final_answer_source` 只可能是 `sdk_final_output` 或共享投影函数
    空终答分支给出的 `accumulated_text`（见 `post_process`）。"""
    result_json = (projection or {}).get('result_json') or {}
    ec = result_json.get('evidence_check') if isinstance(result_json.get('evidence_check'),
                                                        dict) else {}
    return {'run_id': str(run_id or ''), 'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'runtime': SERVING_NAME, 'model': model_provider.MODEL_ID,
            'status': result.status, 'result_status': _result_status(view, result_json),
            'delivery_state': result.delivery_state, 'degraded': result.degraded,
            'model_requests': result.model_requests, 'truncated': result.truncated,
            'elapsed_ms': elapsed_ms,
            'final_answer_source': result_json.get('final_answer_source'),
            'final_answer_branch_reason': result_json.get('final_answer_branch_reason'),
            'refs_n': len(result_json.get('refs') or []),
            'accepted_n': len(ec.get('accepted') or []),
            'rejected_n': len(ec.get('rejected') or []),
            'hidden_in_answer': ec.get('hidden_in_answer'),
            'budget_final': view['budget_final'], 'steps': result.run_context.steps,
            'usage': result.usage, 'error': result.error}


def write_run_ledger(entry: dict, path: Path | str | None = None) -> Path:
    """追加一行到本地 jsonl（写入失败只记日志，不阻断 run）。"""
    p = Path(path) if path else RUN_LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return p


def usage_for_ledger(usage: dict | None) -> dict | None:
    """SDK 聚合 usage → 现行 `UsageLedger` 的记账户名（不第二处抄单价，只做键名搬运）。"""
    if usage is None:
        return None
    return {'prompt_tokens': usage.get('input_tokens'),
            'completion_tokens': usage.get('output_tokens'),
            'total_tokens': usage.get('total_tokens'),
            'prompt_tokens_details': {'cached_tokens': usage.get('cached_input_tokens')},
            'completion_tokens_details': {'reasoning_tokens': usage.get('reasoning_output_tokens')}}


def record_usage(ledger: UsageLedger, result: runtime.AgentRunResult, *,
                 elapsed_ms: int) -> dict:
    """一次 run 一行（**聚合口径**）。"""
    return ledger.record(
        purpose='root_agent', model=model_provider.MODEL_ID,
        usage=usage_for_ledger(result.usage), elapsed_ms=elapsed_ms,
        finish_reason=('run_failed' if result.status == 'failed'
                       else 'max_turns_truncated' if result.truncated else 'stop'),
        note='Agents SDK runtime run 级聚合 usage（run_id=%s，含 %d 次模型请求；'
             '请求数由 raw_responses 长度计，非 usage.requests）'
             % (result.run_id, result.model_requests))


def create_app(*, ctx_fetcher: CtxFetcher, client: ToolServerClient | None = None,
               model: Any | None = None, knowledge_retriever: Any | None = None,
               model_factory: Callable[[], Any] | None = None,
               client_factory: Callable[[], ToolServerClient] | None = None,
               mcp_client_factory: Callable[[str, ToolServerClient], Any] | None = None,
               ledger: UsageLedger | None = None,
               run_ledger_path: Path | str | None = None,
               answer_chunk_chars: int = 120, answer_chunk_delay: float = 0.0,
               display_verify_timeout_s: float = DISPLAY_VERIFY_TIMEOUT_S,
               run_retention: int = RUN_RETENTION) -> FastAPI:
    """创建 HTTP/SSE 服务，注入模型、工具客户端、上下文来源与知识检索器。

每次运行独立登记和缓冲，执行与响应解耦，支持只读恢复已有帧。
ledger=None 时不写费用台账；实时服务装配显式提供账本。"""
    app = FastAPI(title='agents-oagent-serving', version='as02')
    registry = RunRegistry(retention=run_retention)

    @app.get('/healthz')
    def healthz() -> dict:
        return {'ok': True, 'serving': SERVING_NAME, 'schema': WIRE_SCHEMA}

    @app.get('/v1/oagent/ctx')
    async def ctx() -> JSONResponse:
        """返回入口上下文；下游失败时返回明确的上下文错误。"""
        try:
            body = await asyncio.to_thread(ctx_fetcher)
        except Exception as exc:                          # noqa: BLE001  明细只进日志
            log.error('ctx_fetch 失败：%s: %s', type(exc).__name__, exc)
            return JSONResponse({'ok': False, 'error_code': 'ctx_fetch_failed',
                                 'detail': '取上下文失败（明细见服务端日志）'}, status_code=502)
        return JSONResponse(body)

    @app.get('/v1/oagent/run/{run_id}')
    async def resume(run_id: str) -> Response:
        """按稳定 run_id 只读重放已有事件与终态。

不创建任务或调用模型和工具；未知、已淘汰或服务重启前的 ID 返回 404。"""
        record = registry.get(run_id)
        if record is None:
            return JSONResponse({'ok': False, 'error_code': RUN_UNKNOWN,
                                 'detail': '本进程内没有该运行记录（服务可能已重启，或记录已超出'
                                           '本地保留范围）；如需继续请显式重新发起一次调查'},
                                status_code=404)
        return StreamingResponse(_tail(record), media_type='text/event-stream',
                                 headers=SSE_HEADERS)

    @app.post('/v1/oagent/run')
    async def run(request: Request) -> Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({'ok': False, 'error_code': 'invalid_request_body',
                                 'detail': '请求体必须是有效 JSON'}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({'ok': False, 'error_code': 'invalid_request_body',
                                 'detail': '请求体必须是 JSON 对象'}, status_code=400)
        inputs = body.get('inputs') if isinstance(body.get('inputs'), dict) else {}
        run_id = (str(body.get('run_id') or '').strip()
                  or '%s%s' % (RUN_ID_PREFIX, uuid.uuid4().hex[:12]))
        fingerprint = _input_fingerprint(inputs)

        record = registry.get(run_id)
        if record is not None and record.fingerprint != fingerprint:
            # 同 ID 不同输入＝确定性拒绝，不执行、不覆盖原运行（页面提示并让用户显式重开）。
            log.info('run %s 输入冲突：同 ID 已绑定另一次输入，本次不执行', run_id)
            return JSONResponse({'ok': False, 'error_code': RUN_ID_CONFLICT,
                                 'detail': '该运行标识已用于另一次输入；如需新调查请重新开始'},
                                status_code=409)
        if record is None:
            record = registry.create(run_id, fingerprint)
            try:
                record.task = asyncio.create_task(_execute_run(record, inputs))
            except BaseException:
                registry.drop(run_id)
                raise
        return StreamingResponse(_tail(record), media_type='text/event-stream',
                                 headers=SSE_HEADERS)

    async def _tail(record: _RunRecord) -> AsyncIterator[str]:
        """帧缓冲的游标式重放+续看：先重放已有帧，未完成则等新帧，完成后收尾。

        不消费任何执行资源（不发模型/工具请求），因此 POST 复用与 GET 恢复共用同一实现，
        且 GET 天然满足「只读」——它做的事只有读 `record.frames`。"""
        cursor = 0
        while True:
            while cursor < len(record.frames):
                yield record.frames[cursor]
                cursor += 1
            if record.finished:
                return
            await asyncio.sleep(RUN_TAIL_POLL_S)

    async def _execute_run(record: _RunRecord, inputs: dict) -> None:
        """执行一次调查并缓冲所有事件与终态。

        执行协程独立于 SSE 订阅者；连接断开不取消任务，重复提交和恢复请求只重放已有帧。"""
        run_id = record.run_id
        push = record.push
        started = time.monotonic()
        try:
            push(_sse({'event': 'run_started', 'data': {
                'run_id': run_id, 'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z')}}))
            try:
                ctx_body = await asyncio.to_thread(ctx_fetcher)
            except Exception as exc:                      # noqa: BLE001
                log.error('run %s ctx_fetch 失败：%s: %s', run_id, type(exc).__name__, exc)
                push(_failed_frame('ctx_fetch_failed'))
                return
            try:
                asm = assemble_run_input(run_id, inputs, ctx_body)
            except Exception as exc:                      # noqa: BLE001  入口装配失败时明确收尾
                # 入口装配失败只结束当前运行，明细写入日志，不影响服务监听。
                log.error('run %s 入口装配失败：%s: %s', run_id, type(exc).__name__, exc)
                push(_failed_frame('entry_assemble_failed'))
                return
            if not asm['ok']:
                log.info('run %s 入口拒绝 %s：%s', run_id, asm['reject_code'], asm['reject_detail'])
                push(_failed_frame('entry_rejected'))
                return
            run_model = model_factory() if model_factory else model
            run_client = client_factory() if client_factory else client
            if run_model is None or run_client is None:
                # 装配缺失时在流内明确结束，避免留下悬挂响应。
                log.error('run %s 装配缺失：model/model_factory 与 client/client_factory '
                          '至少各提供一项', run_id)
                push(_sse({'event': 'error', 'data': {'reason_class': RUN_ERROR}}))
                push(_failed_frame(RUN_ERROR))
                return

            result: runtime.AgentRunResult | None = None
            try:
                agen = runtime.astream_agent(asm['run_input'], client=run_client,
                                             retriever=knowledge_retriever, model=run_model,
                                             mcp_client_factory=mcp_client_factory)
                async for item in agen:
                    if item.event is not None:
                        for wire in _wire_step_events([item.event]):
                            push(_sse(wire))
                    elif item.result is not None:
                        result = item.result
            except Exception as exc:                      # noqa: BLE001  明细只进日志
                log.exception('run %s Agent 执行异常：%s: %s', run_id, type(exc).__name__, exc)
                push(_sse({'event': 'error', 'data': {'reason_class': RUN_ERROR}}))
                push(_failed_frame(RUN_ERROR))
                return
            if result is None:                            # 生成器合同上必给结果项
                push(_sse({'event': 'error', 'data': {'reason_class': RUN_ERROR}}))
                push(_failed_frame(RUN_ERROR))
                return

            elapsed_ms = int((time.monotonic() - started) * 1000)
            if result.status == 'failed':
                # 初始化失败、缺运行 ID 或空终答时不执行守卫与投影，直接交付失败终态。
                view = run_wire_view(result, {})
                _persist(run_id, result, view, {}, elapsed_ms)
                if (result.error or {}).get('code') == RUN_ERROR:
                    push(_sse({'event': 'error', 'data': {'reason_class': RUN_ERROR}}))
                push(_sse({'event': 'run_finished', 'data': {
                    'status': 'failed', 'answer_md': '',
                    'citations': [], 'result_status': _result_status(view, {}),
                    'hidden_in_answer': None, 'report': None,
                    'knowledge_sources': None,
                    'evidence_details': None, 'display_blocks': None}}))
                return

            projection = await asyncio.to_thread(
                post_process, client=run_client, context=result.run_context,
                run_input=asm['run_input'], final_text=result.final_output)
            result_json = projection.get('result_json') or {}
            result.delivery_state = str(result_json.get('delivery_state') or result.delivery_state)
            view = run_wire_view(result, projection)
            citations, hidden = _citations_wire(result_json)
            _persist(run_id, result, view, projection, elapsed_ms)

            md = result_json['final_answer']
            for i in range(0, len(md), max(1, answer_chunk_chars)):
                push(_sse({'event': 'answer_delta',
                           'data': {'text': md[i:i + answer_chunk_chars]}}))
                if answer_chunk_delay:
                    await asyncio.sleep(answer_chunk_delay)
            # 正文发送完成后再等待展示核验；失败或超时仅影响展示块。
            display_blocks = await finish_display_blocks(
                client=run_client, run_id=run_id, final_text=result.final_output,
                timeout_s=display_verify_timeout_s)
            # 模型再次不输出 display intent（或产了但完全没有 group_totals）时，页面仍须
            # 出现  已算出的多商品品类聚合——用 RunContext 运行期快照做确定性兜底（diff 与
            # value×2 路径都覆盖）；模型自己已给出品类合计时原样使用、不追加。只有问题点名品类
            # （目录品类名/「品类」）才触发，普通多商品比较题不加卡。
            display_blocks = _merge_category_total_fallback(
                display_blocks, getattr(result.run_context, 'category_total_snapshots', None),
                run_id, user_question=str((asm['run_input'] or {}).get('user_question') or ''))
            # /：正文 Guard 结果保持原样；已核验展示事实与正文 accepted 一次合并回查，
            # 让正文未引用的 rank/aux/median 展示依据仍可从 evidence_details 打开。
            evidence_details = await asyncio.to_thread(
                project_evidence_details, client=run_client, run_id=run_id,
                result_json=result_json, display_blocks=display_blocks)
            push(_sse({'event': 'run_finished', 'data': {
                'status': result.status, 'answer_md': md,
                'citations': citations, 'result_status': _result_status(view, result_json),
                'has_business_answer': has_business_answer(result_json),
                # 可选**本轮实际调查范围与比较基准**投影。来源＝运行期台账 `RunContext.query_scopes`（每次成功
                # 查询登记的实际请求成员），产出「调查对象并集 + 指标→成员 + set_compare 基准集合 +
                # cohort 筛选范围」；不同指标实际范围不同时逐指标保留，不合并成单一范围。无成功
                # 查询=缺键（旧 SSE 兼容）。键面单点＝`core.wire_contract.query_scope_wire`。
                'query_scope': query_scope_wire(
                    getattr(result.run_context, 'query_scopes', None)),
                # 可选结构化报告，绑定上面同一份已清洗、经 Guard 处理的终答正文
                # （result_json.final_answer=redact 之后）；不满足最小形状=None（旧 answer_md 路径不变）。
                'hidden_in_answer': hidden,
                # 证据详情不可用时保留正文与引用清单。
                # answer_line 需通过结论头位、长度与逐字匹配检查；有意图区但未给字段时才尝试提取首句。
                # 短结论与展示块分别交付，不互相依赖。
                'report': report_with_answer_line(
                    build_report(str(result_json.get('final_answer') or '')),
                    *display_intent.answer_line_signals(result.final_output)),
                # 知识来源：本 run 通过 knowledge_search 实际返回的文档（doc_id/title/doc_type，
                # 按首次返回顺序去重）。供前端把正文 `[GUIDE01]` 渲染为可点「指南 1」；不进
                # Evidence Guard，不携带正文/片段。无检索成功记录=缺键（旧 SSE 兼容）。
                'knowledge_sources': knowledge_sources_wire(
                    getattr(result.run_context, 'knowledge_sources', None)),
                'evidence_details': evidence_details,
                # 可选类型化展示块（本 run 独立送检+回查+封闭复算后的核验结果，
                # `core.wire_contract.DISPLAY_BLOCKS_SCHEMA` 键面）；无意图/失败/超时=None。
                'display_blocks': display_blocks}}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001  未预期异常也要给终态
            log.exception('run %s 未预期异常：%s: %s', run_id, type(exc).__name__, exc)
            push(_sse({'event': 'error', 'data': {'reason_class': RUN_ERROR}}))
            push(_failed_frame(RUN_ERROR))
        finally:
            registry.finish(record)

    def _persist(run_id: str, result: runtime.AgentRunResult, view: dict, projection: dict,
                 elapsed_ms: int) -> None:
        """per-run 登记 + usage 计费记账（本地 out/；失败只记日志，不改变终态）。

        只在 `_execute_run` 里调用一次——复用同一 ID 的订阅者不重复登记、不重复计费。"""
        try:
            write_run_ledger(_ledger_entry(run_id, result, view, projection, elapsed_ms),
                             run_ledger_path)
        except OSError as exc:
            log.warning('run %s per-run 登记落盘失败：%s: %s', run_id,
                        type(exc).__name__, exc)
        if ledger is None:
            return
        if result.usage is None:
            return
        try:
            record_usage(ledger, result, elapsed_ms=elapsed_ms)
        except OSError as exc:
            log.warning('run %s usage 记账失败：%s: %s', run_id, type(exc).__name__, exc)

    # 进程内 run 登记表挂在 app.state 上（同一 app 一个登记表；测试与本地排查可只读查看）。
    app.state.run_registry = registry
    return app


# ---------- stub 装配（零真机、零模型调用、零 :8099 触碰） ----------

def scripted_model(turns: list[dict], *, tokens: tuple[int, int] = (100, 20)) -> Any:
    """轮次脚本形状（`{'content', 'tool_calls'}`，与 `current_demo_turns` 同源）→ SDK
    `ScriptedModel` 步骤；仅 stub 装配与本地验收使用（SDK 自带确定性替身，非 provider 适配层）。

    一轮=该轮 assistant 正文（可空）+ 若干工具调用；usage 给固定假值（只证合同，不是真机 token）。
    """
    from agents.testing import ScriptedModel, assistant_message, function_call
    from agents.usage import Usage

    usage = Usage(input_tokens=tokens[0], output_tokens=tokens[1],
                  total_tokens=tokens[0] + tokens[1])
    steps: list[dict] = []
    seq = 0
    for turn in turns:
        items: list[Any] = []
        text = str(turn.get('content') or '')
        if text:
            items.append(assistant_message(text))
        for call in turn.get('tool_calls') or []:
            seq += 1
            items.append(function_call(str(call['name']), call.get('arguments') or {},
                                       call_id=str(call.get('id') or 'c%d' % seq)))
        steps.append({'output': items, 'usage': usage})
    return ScriptedModel(steps)


def current_demo_turns() -> list[dict]:
    """current 工具面演示脚本（零模型桩用）：取值 → 跨期比较 → 集合排名 → 结构化终答。"""
    from .stubs import PERIOD_P1, PERIOD_P2, PRODUCT_DEMO, PRODUCT_POOL
    pool_ids = [p['product_id'] for p in PRODUCT_POOL]

    def call(name: str, **kwargs) -> dict:
        return {'name': name, 'arguments': kwargs}

    return [
        {'content': '先查分析对象两期的核心指标现状，再决定是否补查。',
         'tool_calls': [call('get_metric_values',
                             scope={'kind': 'product_ids', 'product_ids': [PRODUCT_DEMO]},
                             metrics=['M1', 'M2'], period_id=PERIOD_P1)]},
        {'content': '',
         'tool_calls': [call('compare_periods',
                             scope={'kind': 'product_ids', 'product_ids': [PRODUCT_DEMO]},
                             metric='M1',
                             base_period_id=PERIOD_P1, target_period_id=PERIOD_P2)]},
        {'content': '',
         'tool_calls': [call('compare_product_set',
                             scope={'kind': 'product_ids', 'product_ids': list(pool_ids)},
                             metric='M1',
                             kind='rank', period_id=PERIOD_P1)]},
        {'content': (
            '## 结论\n'
            '分析对象两期订单下滑明显；结合单期读数与全池对照，当前优先处理曝光侧收缩，'
            '而不是素材质量本身 [E3]。\n\n'
            '## 关键发现\n'
            '- M1 订单量：基期 3900 单 → 对比期 2600 单，变化 -1300 单（-33.3%）[E3]。\n'
            '- M2 播放量：基期 260000；对比期读数本次未取到（unknown），不外推 [E2]。\n'
            '- 全池 M1 单期对照基准 3900 单，样本=目录内商品（Demo 口径）[E4]。\n\n'
            '## 下一步\n'
            '- 先核对曝光收缩是否伴随发布频次下降，再决定是否补内容 [E3]。\n'
            '- 若成立，优先恢复内容供给并观察下一窗口，暂不动价格。\n\n'
            '## 重要限制\n'
            '- 以上为两期窗口内的变化，不外推长期趋势；机制因果未被证明。\n'
            '- 引用核验只证明引用真实存在，不自动证明整句结论；数值口径以工具返回为准。'),
         'tool_calls': []},
    ]


def build_stub_app(*, turns: list[dict] | None = None, knowledge_retriever: Any | None = None,
                   transport: Any | None = None, **kwargs: Any) -> FastAPI:
    """使用 ScriptedModel、FakeToolServer 与本地上下文创建测试服务。

演示脚本覆盖取值、跨期比较与排名；MCP 会话与 HTTP 客户端共用同一运行状态。
可注入轮次、传输与检索器以验证失败路径，不写实际模型费用。"""
    from core.data import oagent_ctx

    from .stubs import FakeMCPQueryClient, FakeToolServer

    script = current_demo_turns() if turns is None else turns
    return create_app(
        model_factory=lambda: scripted_model(script),
        client_factory=lambda: ToolServerClient(transport=transport or FakeToolServer()),
        mcp_client_factory=lambda run_id, tool_client: FakeMCPQueryClient(run_id, tool_client),
        ctx_fetcher=oagent_ctx.ctx_fetch,
        knowledge_retriever=knowledge_retriever,
        **kwargs)


def main() -> None:
    import os

    import uvicorn

    port = int(os.environ.get('AGENTS_OAGENT_SERVING_PORT') or '8327')
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    log.info('Agents SDK oagent 服务层（stub 装配）启动：127.0.0.1:%d', port)
    uvicorn.run(build_stub_app(), host='127.0.0.1', port=port, log_level='warning')


if __name__ == '__main__':
    main()
