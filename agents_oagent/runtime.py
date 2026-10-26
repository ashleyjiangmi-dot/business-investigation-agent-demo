# -*- coding: utf-8 -*-
"""Root Agent 装配与 SDK 流式执行。

由 SDK Runner 承担模型请求、工具调用与结果回填。运行前初始化根任务，
运行中登记预算与事件；达到轮次上限时保留已有终答并登记截断。
系统指令来自 CURRENT_ROOT_INSTRUCTION，用户输入来自 entry_adapter，终答取 final_output。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from agents import Agent, MaxTurnsExceeded, Runner
from agents.items import ItemHelpers, MessageOutputItem, RunItem
from agents.models.interface import Model
from agents.run_config import RunConfig, ToolExecutionConfig
from agents.run_error_handlers import RunErrorHandlerInput, RunErrorHandlerResult
from agents.usage import Usage

from core.prompts import CURRENT_ROOT_INSTRUCTION
from core.root_contract import MAX_MODEL_REQUESTS
from core.tool_client import ToolHttpError, ToolServerClient

from . import model_provider
from .context import RunContext
from .mcp_query_tools.client import MCPQueryClient
from .tools import (MCP_QUERY_INPUT_GUARDRAIL, MCP_QUERY_OUTPUT_GUARDRAIL, build_tools,
                    tool_error_text)

AGENT_NAME = 'root_agent'
WORKFLOW_NAME = 'agents_oagent_root'
RUN_ERROR = 'run_error'


@dataclass
class AgentRunResult:
    """一次 run 的结果（交接物：终答 + run context + SDK usage）。"""

    run_id: str
    status: str                     # succeeded | failed
    final_output: str
    run_context: RunContext
    usage: dict | None = None
    model_requests: int = 0         # SDK 实际完成的模型请求数（=turn 数）
    truncated: bool = False
    # normal | truncated_degraded | evidence_unverified | truncated_evidence_unverified
    delivery_state: str = 'normal'
    error: dict | None = None

    @property
    def degraded(self) -> bool:
        return self.delivery_state != 'normal'

    def to_dict(self) -> dict:
        """可进证据件的投影（事件/步骤/预算账目原样；不含模型 raw items）。"""
        context = self.run_context
        return {'run_id': self.run_id, 'status': self.status, 'final_output': self.final_output,
                'model_requests': self.model_requests, 'truncated': self.truncated,
                'degraded': self.degraded, 'delivery_state': self.delivery_state,
                'usage': self.usage, 'error': self.error,
                'budget_final': context.budget_final(), 'steps': context.steps,
                'truncation': context.truncation}


def usage_snapshot(usage: Usage) -> dict | None:
    """SDK 聚合 usage → 项目记账户名（input/output_tokens 即 prompt/completion 语义）。

    请求数不从 `usage.requests` 取（本包实测该字段不随 Chat Completions 聚合自增，避免误读），
    由 `RunResult.raw_responses` 长度计=模型请求数。SDK 未积累任何 token 用量时返回 None；
    默认零值不等同于 provider 报告了零 token。
    """
    observed = (usage.input_tokens, usage.output_tokens, usage.total_tokens,
                usage.input_tokens_details.cached_tokens,
                usage.output_tokens_details.reasoning_tokens)
    if not any(value is not None and value > 0 for value in observed):
        return None
    return {'input_tokens': int(usage.input_tokens or 0),
            'output_tokens': int(usage.output_tokens or 0),
            'total_tokens': int(usage.total_tokens or 0),
            'cached_input_tokens': int(usage.input_tokens_details.cached_tokens or 0),
            'reasoning_output_tokens': int(usage.output_tokens_details.reasoning_tokens or 0)}


def build_agent(model: Model | None = None, mcp_servers: list | None = None) -> Agent:
    """装配单个 Root Agent，包含 oagent_query、knowledge_search 与已连接的 MCP 查询会话。

    SDK 负责发现和调用四个 MCP 工具；未传会话时不挂载这些工具。"""
    return Agent(name=AGENT_NAME,
                 instructions=CURRENT_ROOT_INSTRUCTION,
                 model=model if model is not None else model_provider.build_model(),
                 model_settings=model_provider.model_settings(),
                 tools=build_tools(),
                 mcp_servers=list(mcp_servers or []))


def build_run_config() -> RunConfig:
    """串行 Tool 执行（不改现行预算语义）+ 关 SDK tracing + 工具面外调用返回结构化拒绝。"""
    return RunConfig(workflow_name=WORKFLOW_NAME,
                     tracing_disabled=True,
                     tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1),
                     tool_not_found_behavior='return_error_to_model',
                     tool_error_formatter=tool_error_text)


def _last_assistant_text(new_items: list[RunItem]) -> str:
    for item in reversed(new_items or []):
        if isinstance(item, MessageOutputItem):
            return ItemHelpers.text_message_output(item)
    return ''


def _on_max_turns(handler_input: RunErrorHandlerInput) -> RunErrorHandlerResult:
    """轮次上限=截断收尾（不猜结局）：登记 truncation + 末条 assistant 文本作为终答形状。"""
    context: RunContext = handler_input.context.context
    context.truncation = {'truncated': True, 'reason': 'max_turns', 'max': MAX_MODEL_REQUESTS,
                          'note': 'SDK 形态：完成第 6 次模型请求后、发起第 7 次前截断；'
                                  '保留已生成的末条 assistant 文本并登记截断'}
    context.add_event({'type': 'max_turns_truncated', 'max': MAX_MODEL_REQUESTS})
    return RunErrorHandlerResult(final_output=_last_assistant_text(
        list(handler_input.run_data.new_items or [])), include_in_history=True)


def root_init_failure(run_id: str, resp: Any) -> dict:
    """run 前初始化失败 → 与 对照实现 root_init 节点同形的错误登记"""
    return {'stage': 'root_init', 'code': str((resp or {}).get('error_code') or 'unknown'),
            'detail': str((resp or {}).get('detail') or ''),
            'root_init_response': resp}


@dataclass
class StreamItem:
    """`astream_agent` 的一条产出：`event`=run 内新增事件（服务层过白名单后下发），
    `result`=本次 run 的结果。一条产出只带其中一个，`result` 只在末条出现。"""

    event: dict | None = None
    result: AgentRunResult | None = None


async def _open_mcp(factory, run_id: str, client: ToolServerClient) -> tuple[Any | None, dict | None]:
    """为已初始化的根任务建立 MCP 会话并接入查询前后守卫。

    factory 可注入进程内替身；连接失败明确结束运行，不切换查询入口。"""
    session = None
    try:
        session = factory(run_id, client) if factory else MCPQueryClient(run_id)
        session.configure_guardrails(input_guardrails=[MCP_QUERY_INPUT_GUARDRAIL],
                                     output_guardrails=[MCP_QUERY_OUTPUT_GUARDRAIL])
        await session.start()
        return session, None
    except asyncio.CancelledError:
        # CancelledError 不属于 Exception；连接期间取消时须先关闭半开会话，再继续抛出取消。
        await _close_mcp(session)
        raise
    except Exception as exc:                  # noqa: BLE001  MCP 起服/握手/合同核对失败
        await _close_mcp(session)
        return None, {'stage': 'mcp_connect', 'code': 'mcp_connect_failed',
                      'detail': '%s: %s' % (type(exc).__name__, exc)}


async def _close_mcp(session: Any | None) -> None:
    if session is None:
        return
    try:
        await session.close()
    except Exception:                          # noqa: BLE001  收尾失败不覆盖 run 结果
        pass


async def astream_agent(run_input: Mapping[str, Any], *, client: ToolServerClient,
                        retriever: Any | None = None, model: Model | None = None,
                        max_turns: int = MAX_MODEL_REQUESTS,
                        mcp_client_factory: Any | None = None) -> AsyncIterator[StreamItem]:
    """流式跑一次根 Agent loop（服务层入口）。

    事件下发面只转发 `RunContext.events` 的新增量（=工具 wrapper 在真实执行时刻登记的
    step_started/step_finished），不消费 SDK 的 raw response 事件——SDK 流事件在此只承担两件事：
    ①让上游 Chat Completions 走 `stream=True`，避免等待完整响应时触发中转站空闲超时；
    ②给事件之间的下发时机。非步骤事件（root_init_ok/max_turns_truncated/…）由服务层的
    白名单过滤丢弃，不在本模块判定。
    """
    run_id = str(run_input.get('run_id') or '').strip()
    if not run_id:
        yield StreamItem(result=AgentRunResult(
            run_id='', status='failed', final_output='',
            run_context=RunContext(run_id='', client=client, retriever=retriever),
            error={'stage': 'root_init', 'code': 'root_run_id_missing',
                   'detail': '根任务 run_id 缺失（客户端预检，不发起请求）：由入口注入，模型不可填'}))
        return

    context = RunContext(run_id=run_id, client=client, retriever=retriever)
    cursor = 0

    try:
        init_kwargs = {}
        if run_input.get('allowed_product_ids'):
            init_kwargs['allowed_product_ids'] = list(run_input.get('allowed_product_ids') or [])
        init = await asyncio.to_thread(client.root_init, run_id,
                                      list(run_input.get('allowed_periods') or []),
                                      dict(run_input.get('expected_versions') or {}),
                                      **init_kwargs)
    except ToolHttpError as exc:
        yield StreamItem(result=AgentRunResult(
            run_id=run_id, status='failed', final_output='', run_context=context,
            error={'stage': 'root_init', 'code': 'tool_http_error', 'detail': str(exc)}))
        return
    if not init.get('ok'):
        yield StreamItem(result=AgentRunResult(
            run_id=run_id, status='failed', final_output='', run_context=context,
            error=root_init_failure(run_id, init)))
        return
    context.note_server_budget(init)
    context.add_event({'type': 'root_init_ok', 'event': str(init.get('event') or '')})
    while cursor < len(context.events):
        yield StreamItem(event=context.events[cursor])
        cursor += 1

    # 根任务初始化成功后再创建绑定该任务的 MCP 会话；会话不可用时结束运行。
    mcp_session, mcp_error = await _open_mcp(mcp_client_factory, run_id, client)
    if mcp_error is not None:
        yield StreamItem(result=AgentRunResult(
            run_id=run_id, status='failed', final_output='', run_context=context,
            error=mcp_error))
        return
    context.mcp = mcp_session
    try:
        context.add_event({'type': 'mcp_ready',
                           'tools': list(getattr(mcp_session, 'tool_names', None) or [])})
        while cursor < len(context.events):
            yield StreamItem(event=context.events[cursor])
            cursor += 1

        agent = build_agent(model, mcp_servers=[mcp_session.server])
        streamed = Runner.run_streamed(agent,
                                       [{'role': 'user',
                                         'content': str(run_input.get('agent_input') or '')}],
                                       context=context, max_turns=max_turns,
                                       run_config=build_run_config(),
                                       error_handlers={'max_turns': _on_max_turns})
        try:
            async for _sdk_event in streamed.stream_events():
                while cursor < len(context.events):   # 工具串行执行：此处取到的步序即调用序
                    yield StreamItem(event=context.events[cursor])
                    cursor += 1
        except MaxTurnsExceeded as exc:  # 仅在调用方未接 error_handlers 的形态下到达这里
            context.truncation = {'truncated': True, 'reason': 'max_turns', 'max': max_turns,
                                  'note': 'MaxTurnsExceeded 未被 handler 接住：%s' % exc}
            yield StreamItem(result=AgentRunResult(
                run_id=run_id, status='failed', final_output='', run_context=context,
                truncated=True,
                delivery_state='truncated_degraded',
                error={'stage': 'agent_loop', 'code': 'max_turns_exceeded', 'detail': str(exc)}))
            return
        except Exception as exc:
            # SDK streaming result keeps the run context and completed responses when
            # stream_events raises; preserve only usage the SDK has accumulated so far. The
            # failed request itself may have been billed, but its usage is unavailable here and
            # must not be estimated.
            while cursor < len(context.events):
                yield StreamItem(event=context.events[cursor])
                cursor += 1
            yield StreamItem(result=AgentRunResult(
                run_id=run_id, status='failed', final_output='', run_context=context,
                usage=usage_snapshot(streamed.context_wrapper.usage),
                model_requests=len(streamed.raw_responses or []),
                error={'code': RUN_ERROR, 'exception_type': type(exc).__name__,
                       'detail': str(exc)}))
            return

        while cursor < len(context.events):  # 流末余量（截断收尾事件在最后一条 SDK 流事件之后追加）
            yield StreamItem(event=context.events[cursor])
            cursor += 1

        # 空终答记为失败，不填默认答案或自动重试；截断后仍有正文时保留可交付答案。
        final_text = str(streamed.final_output or '')
        truncated = context.truncation is not None
        if final_text.strip():
            status, error = 'succeeded', None
        else:
            status = 'failed'
            error = {'stage': 'agent_loop', 'code': 'empty_final_answer',
                     'detail': '最终可见答案为空（strip 后 0 字），不作为正常成功交付登记'
                               + ('；此前到达轮次上限（max_turns=%d），末轮未产出文本' % max_turns
                                  if truncated else '；模型结束但未产出可见文本')}
        yield StreamItem(result=AgentRunResult(
            run_id=run_id, status=status,
            final_output=final_text,
            run_context=context, usage=usage_snapshot(streamed.context_wrapper.usage),
            model_requests=len(streamed.raw_responses or []),
            truncated=truncated,
            delivery_state='truncated_degraded' if truncated else 'normal',
            error=error))
    finally:
        # 会话生命周期＝根任务生命周期：任何出口（正常收尾 / 生成器被提前关闭 / 运行期取消）
        # 都关掉子进程。保护区自会话建立后立即开始（含 mcp_ready 下发），不留时间窗。
        await _close_mcp(mcp_session)


async def arun_agent(run_input: Mapping[str, Any], *, client: ToolServerClient,
                     retriever: Any | None = None, model: Model | None = None,
                     max_turns: int = MAX_MODEL_REQUESTS,
                     mcp_client_factory: Any | None = None) -> AgentRunResult:
    """非流式入口（脚本/单测用）：排空同一条流式实现、丢弃中间事件。
    `astream_agent` 的每条出口路径都以结果项收尾；生成器未按合同给出结果项时显式报
    合同违规（否则 result 未绑定会以 UnboundLocalError 掩盖真实成因）。"""
    result: AgentRunResult | None = None
    async for item in astream_agent(run_input, client=client, retriever=retriever,
                                    model=model, max_turns=max_turns,
                                    mcp_client_factory=mcp_client_factory):
        if item.result is not None:
            result = item.result
    if result is None:
        raise RuntimeError('astream_agent 生成器提前终止：未按合同产出结果项（run_id=%r）'
                           % (str(run_input.get('run_id') or ''),))
    return result


def run_agent(run_input: Mapping[str, Any], *, client: ToolServerClient,
              retriever: Any | None = None, model: Model | None = None,
              max_turns: int = MAX_MODEL_REQUESTS,
              mcp_client_factory: Any | None = None) -> AgentRunResult:
    """同步入口（脚本/单测用；事件循环内请用 `arun_agent`）。"""
    return asyncio.run(arun_agent(run_input, client=client, retriever=retriever,
                                  model=model, max_turns=max_turns,
                                  mcp_client_factory=mcp_client_factory))
