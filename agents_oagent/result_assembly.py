# -*- coding: utf-8 -*-
"""终答清洗、引用核验、正文投影与展示结果装配。

post_process 处理正文；finish_display_blocks 完成展示核验；
_merge_category_total_fallback 装配品类合计；project_evidence_details 回查原始证据。
只通过注入的工具客户端做只读查询，不构造模型；HTTP/SSE 与运行登记由 server 负责。"""
from __future__ import annotations

import asyncio
import json
import logging

from core.evidence_contract import extract_refs, isolate_unverified_claims, projection_main
from core.tool_client import ToolServerClient
from core.wire_contract import DISPLAY_BLOCKS_SCHEMA, evidence_details_wire

from . import display_intent, final_answer_clean, runtime

log = logging.getLogger('agents_oagent.result_assembly')

FINAL_ANSWER_SOURCE = 'sdk_final_output'   # 新链终答事实源（不伪造平台逐轮语义 last_round）


def post_process(*, client: ToolServerClient, context: runtime.RunContext,
                 run_input: dict, final_text: str) -> dict:
    """终答之后的确定性收尾：终答清洗 → 引用提取 → 守卫 → 投影 →
    局部隔离标注（只贴局部提示、不动审计面）。"""
    rid = context.run_id
    parsed = display_intent.parse_display_intent(final_text)
    business_text = parsed['business_text']
    # 展示层清洗：业务正文 → 用户可见正文（剥掉思维链残片/英文 scratchpad/过程叙述段）。
    # raw_answer 仍登记原文（agent_text 未清洗）供排查；引用与守卫只看清洗后的可见正文。
    visible_text = final_answer_clean.clean_final_answer(business_text)
    # 清洗正文并剥离展示意图后、写入占位文案前判断业务正文是否存在。
    # 该判据用于完成状态；占位内容不参与判断。
    has_business_answer = bool(visible_text.strip())
    branch_reason = ''
    if final_text.strip() and not has_business_answer:
        # 清洗后无可展示正文 ⇒ 可见面不落 raw reasoning，改走明确的"未生成可展示最终回答"文案；
        # 诊断态记入既有 final_answer_branch_reason 槽位，原始输出恒在 result_json.raw_answer。
        # 两种成因分开登记：剥掉标记区后本就无业务正文（只给了意图区）＝intent_only_answer；
        # 有正文但全部被判为推理过程＝reasoning_only_hidden（既有语义，不合并口径）。
        if parsed['marker_zone_stripped'] and not business_text.strip():
            branch_reason = display_intent.BRANCH_INTENT_ONLY
        else:
            branch_reason = final_answer_clean.BRANCH_REASONING_ONLY
        visible_text = final_answer_clean.NO_DISPLAYABLE_ANSWER
    refs = extract_refs(visible_text)
    try:
        guard = client.qev_guard(rid or '__missing__', refs)
    except Exception:                                    # noqa: BLE001  未核验如实登记，不代 0
        guard = None
    else:
        context.note_server_budget(guard)    # 用服务端预算回显校准本地镜像。
    # 单 Agent 不读取专项状态，投影将其明确登记为不可用。
    out = projection_main(
        agent_text=final_text,
        guard_body='' if guard is None else json.dumps(guard, ensure_ascii=False),
        refs_json=json.dumps(list(refs), ensure_ascii=False),
        run_id=rid,
        entry_meta_json=json.dumps(run_input.get('entry_meta') or {}, ensure_ascii=False),
        selection_json=json.dumps(run_input.get('selection') or {}, ensure_ascii=False),
        user_question=str(run_input.get('user_question') or ''),
        specialist_records_json='',
        final_text=visible_text,
        final_source=FINAL_ANSWER_SOURCE,
        branch_reason=branch_reason,
        has_business_answer=has_business_answer)
    result = out['result_json']
    # Guard 后局部隔离只改规范业务正文；流式、终答与报告使用同一正文。
    final_answer, isolated_n = isolate_unverified_claims(result['final_answer'])
    result['final_answer'] = final_answer
    out['projection_md'] = final_answer
    evidence_check = result['evidence_check']
    accepted_n = len(evidence_check.get('accepted') or [])
    citation_anomaly = bool(
        evidence_check.get('rejected')
        or evidence_check.get('foreign_namespace')
        or int(evidence_check.get('hidden_in_answer') or 0) > 0)
    if evidence_check.get('guard_unavailable'):
        evidence_degraded = True
    elif citation_anomaly:
        evidence_degraded = accepted_n == 0
    else:
        evidence_degraded = evidence_check.get('ok') is not True
    truncated = context.truncation is not None
    if truncated and evidence_degraded:
        delivery_state = 'truncated_evidence_unverified'
    elif truncated:
        delivery_state = 'truncated_degraded'
    elif evidence_degraded:
        delivery_state = 'evidence_unverified'
    else:
        delivery_state = 'normal'
    result['delivery_state'] = delivery_state
    result['degraded'] = delivery_state != 'normal'
    context.add_event({'type': 'refs_extracted', 'ref_count': len(refs),
                       'final_source': FINAL_ANSWER_SOURCE,
                       'guard_ok': None if guard is None else bool(guard.get('ok')),
                       #  局部隔离读数（内部排查件，白名单不过线）：被标记的最小单元数
                       # （≠ hidden_in_answer——那是审计面的隐藏引用处数，两者不合并口径）。
                       'isolated_n': isolated_n})
    # 内部排查件（不过线：非步骤事件由 `_wire_step_events` 白名单丢弃）。登记正文/意图分离
    # 三态与块数，供本地运行排查；展示意图核验读数在 run 流收尾处另行装配。
    context.add_event({'type': 'display_intent_parse', 'status': parsed['status'],
                       'note': parsed['note'],
                       'blocks': len((parsed.get('intent') or {}).get('blocks') or [])})
    return out


def project_evidence_details(*, client: ToolServerClient, run_id: str,
                             result_json: dict,
                             display_blocks: dict | None = None) -> list | None:
    """只读回查正文已接受引用与展示块已核验事实的本 run 原始证据。

    展示块由 `build_display_blocks` 先经 Evidence Guard/resolve 核验；此处仅合并其
    `verified_facts`、`recomputed.rankings` 和 `recomputed.tool_returned` 中的真实 ref/handle，
    与正文 `evidence_check.accepted` 一次回查。展示引用不改写正文 Guard、citations 或
    delivery_state，也不发起经营查询、不改变预算与登记面。端点失败/根任务不存在=返回 None，
    前端回退到证据清单视图，终答交付不受影响。"""
    ec = (result_json or {}).get('evidence_check') or {}
    if not isinstance(ec, dict):
        ec = {}
    accepted = [str(r) for r in (ec.get('accepted') or [])]
    seen = set(accepted)
    handle_resolutions = dict(ec.get('handle_resolutions') or {})
    blocks = (display_blocks or {}).get('blocks') if isinstance(display_blocks, dict) else None
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        refs = []
        facts = block.get('verified_facts')
        if isinstance(facts, list):
            refs.extend(facts)
        recomputed = block.get('recomputed')
        if isinstance(recomputed, dict):
            for key in ('rankings', 'tool_returned'):
                items = recomputed.get(key)
                if isinstance(items, list):
                    refs.extend(items)
        for item in refs:
            if not isinstance(item, dict):
                continue
            ref, handle = item.get('ref'), item.get('handle')
            if not ref or not handle:
                continue
            canonical = str(ref)
            if canonical not in seen:
                accepted.append(canonical)
                seen.add(canonical)
            handle_resolutions.setdefault(str(handle), canonical)
    if not accepted:
        return []
    detail_ec = {**ec, 'accepted': accepted,
                 'handle_resolutions': handle_resolutions}
    try:
        resp = client.evidence_resolve(str(run_id or ''), accepted)
    except Exception:                             # noqa: BLE001  详情不可得=降级，不阻断终答
        log.info('run %s evidence_resolve 失败：evidence_details 降级为 None', run_id)
        return None
    return evidence_details_wire(detail_ec, resp)


async def finish_display_blocks(*, client: ToolServerClient, run_id: str,
                               final_text: str, timeout_s: float) -> dict | None:
    """ 步骤 5：展示意图核验后置收尾（run 流在 `answer_delta` 全部发出后调用）。

    同步装配放进 worker 线程（guard+resolve 是两个只读 HTTP 往返），并以 `timeout_s`
    封顶对 `run_finished` 的额外等待；超时=放弃展示、整键 null。装配内部的失败降级
    （无意图/解析失败/空块/账本失效/通道异常）已在 `build_display_blocks` 收口为 None。
    本收尾**不改变** `delivery_state` 正文证据语义、不登记 SSE 步骤、不新增事件类型。
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(display_intent.build_display_blocks,
                              client=client, run_id=run_id, final_text=final_text),
            timeout=timeout_s)
    except asyncio.TimeoutError:
        log.info('run %s 展示意图核验超时（>%.1fs）：display_blocks 降级为 null（正文交付不受影响）',
                 run_id, timeout_s)
        return None


def _merge_category_total_fallback(display_blocks: dict | None, snapshots,
                                   run_id: str, user_question: str = '') -> dict | None:
    """品类问题缺少 group_totals 展示块时，将已登记的品类合计快照转为比较块。

    不新增查询或修改正文；普通多商品比较和无有效快照时不补卡片。
    未预期异常＝保留原 display_blocks 并记日志（该函数在 run 主 try 内、`finish_display_blocks`
    之外调用，异常隔离在这里做——不把已发完正文的 run 打成 RUN_ERROR）。"""
    try:
        blocks = (display_blocks or {}).get('blocks') if isinstance(display_blocks, dict) else None
        existing = [b for b in (blocks or []) if isinstance(b, dict)]

        def _has_totals(b: dict) -> bool:
            rec = b.get('recomputed') if isinstance(b.get('recomputed'), dict) else {}
            totals = rec.get('group_totals')
            return isinstance(totals, list) and len(totals) > 0

        if any(_has_totals(b) for b in existing):
            return display_blocks
        fallback = display_intent.category_total_fallback_blocks(
            snapshots, run_id, start_index=len(existing), user_question=user_question)
        if not fallback:
            return display_blocks
        if not isinstance(display_blocks, dict):
            return {'schema': DISPLAY_BLOCKS_SCHEMA, 'run_id': str(run_id or ''),
                    'parse_status': 'deterministic_fallback', 'blocks': fallback}
        return {**display_blocks, 'blocks': list(blocks or []) + fallback}
    except Exception:                                 # noqa: BLE001  兜底失败只弃兜底,不动已核验块
        log.exception('run %s 品类合计兜底装配异常：保留原 display_blocks', run_id)
        return display_blocks
