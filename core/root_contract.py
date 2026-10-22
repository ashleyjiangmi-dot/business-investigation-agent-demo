# -*- coding: utf-8 -*-
"""根 Agent 的预算、轮次上限、工具响应分类与知识检索合同。

只维护常量、判据与响应形状，不承载 Agent 循环、运行状态或展示投影。
预算上限来自 operating_agent_brief；查询尝试含被拒、重复与缓存命中。
运行时按实际开放的工具族装配预算视图。"""
from __future__ import annotations

import json
from typing import Any

from .data import operating_agent_brief as OAB  # noqa: F401  （OAB 供本文件预算常量取数）

from .knowledge import DOC_TYPES, GOVERNANCE_FIELDS   # noqa: E402

MAX_MODEL_REQUESTS = 6
BUDGET_LIMITS = {'queries': OAB.OAGENT_ROOT_BUDGET['period_query_attempts'],
                 'evidence': OAB.OAGENT_ROOT_BUDGET['evidence_calls'],
                 'specialist': OAB.OAGENT_ROOT_BUDGET['specialist_calls']}
OAGENT_CONTRACT_VERSION = 'oagent_root_v1'   # 与 oagent_ctx.OAGENT_CONTRACT_VERSION 同值

_BUDGET_REJECT_DETAIL = {
    'queries': '根任务周期查询尝试已达上限 %d（含被拒与重复；整体调用限制不可绕过）',
    'evidence': '根任务单期核对调用已达上限 %d 次（整体调用限制）',
    'specialist': '根任务专项调用已达上限 %d 次（整体调用限制）',
}
_BUDGET_REJECT_CODE = {'queries': 'budget_reached', 'evidence': 'root_evidence_budget_reached',
                       'specialist': 'specialist_budget_reached'}


# ---------- 预算：先查后扣（判据单点） ----------

# 预算视图按调用方指定的工具族生成；当前 Root Agent 仅使用 queries 预算。
BUDGET_VIEW_FAMILIES = ('queries', 'evidence', 'specialist')


def local_budget_view(budget: dict, families: tuple = BUDGET_VIEW_FAMILIES) -> dict:
    """生成本地预算视图；families 指定需要展示的工具族，查询族始终包含在内。"""
    out = {'queries_attempted': int(budget.get('queries') or 0),
           'queries_attempted_max': BUDGET_LIMITS['queries']}
    if 'evidence' in families:
        out['evidence_calls'] = int(budget.get('evidence') or 0)
        out['evidence_calls_max'] = BUDGET_LIMITS['evidence']
    if 'specialist' in families:
        out['specialist_calls'] = int(budget.get('specialist') or 0)
        out['specialist_calls_max'] = BUDGET_LIMITS['specialist']
    out['note'] = ('客户端预算镜像：先查后扣，被拒/重复/缓存命中同样计尝试；'
                   '服务端 root_budget 为权威计数')
    return out
    # 注：period_count（成功查询镜像）属服务端 pctx 统计，客户端不镜像（无法可靠对齐）。


def budget_gate_decision(family: str | None, budget: dict) -> dict:
    """三族预算代码闸（机械，非 prompt 自觉）。family=None → unknown_tool。

    返回 {'allowed': bool, 'code': ..., 'detail': ...}。语义对照 oagent_ctx：
    先查后扣——计数 >= 上限即拒绝且**不计数**（与 execute_root_query 检查在扣减之前一致）。
    """
    if family is None:
        return {'allowed': False, 'code': 'unknown_tool',
                'detail': '未知工具：主链工具列表只含 oagent_query/oagent_evidence（PC01 单 Agent 合同）'}
    used = int(budget.get(family) or 0)
    limit = BUDGET_LIMITS[family]
    if used >= limit:
        return {'allowed': False, 'code': _BUDGET_REJECT_CODE[family],
                'detail': _BUDGET_REJECT_DETAIL[family] % limit}
    return {'allowed': True, 'code': '', 'detail': ''}


def budget_reject_response(family: str, detail: str, budget: dict,
                           families: tuple = BUDGET_VIEW_FAMILIES) -> dict:
    """生成额度超限响应，并按 families 限定返回的预算字段。"""
    view = local_budget_view(budget, families)
    if family == 'queries':
        return {'event': 'budget_reached', 'budget_kind': 'root_period_query_attempts',
                'contract_version': OAGENT_CONTRACT_VERSION, 'detail': detail,
                'root_budget': view}
    return {'ok': False, 'error_code': _BUDGET_REJECT_CODE[family], 'detail': detail,
            'root_budget': view}


# ---------- 工具响应分类与步骤摘要 ----------

# ---------- 模型可见响应终稿（单点） ----------

# 模型只能使用查询族预算，响应中不展示取证和专项额度，避免规划不存在的工具调用。
_ROOT_BUDGET_LEGACY_SLOTS = ('evidence_calls', 'evidence_calls_max',
                             'specialist_calls', 'specialist_calls_max')
# 模型响应过滤内部实现标签，并使用当前查询族的额度说明。
_MODEL_VISIBLE_DROP_KEYS = ('rejected_by',)
_BUDGET_NOTE_LEGACY = '不再发取数/取证/专项请求'
_BUDGET_NOTE_CURRENT = '不再发数据查询请求'


def model_visible_response(response: Any) -> Any:
    """复制并清理模型可见工具响应，移除非查询族预算与内部实现标签。

    所有操作与成败路径使用相同过滤；服务端登记的原始响应不变。
    保留查询额度、有效证据引用及业务错误信息。"""
    if not isinstance(response, dict):
        return response
    out = response
    budget = out.get('root_budget')
    if isinstance(budget, dict):
        slim = {key: value for key, value in budget.items()
                if key not in _ROOT_BUDGET_LEGACY_SLOTS}
        note = slim.get('note')
        if isinstance(note, str):
            slim['note'] = note.replace(_BUDGET_NOTE_LEGACY, _BUDGET_NOTE_CURRENT)
        out = {**out, 'root_budget': slim}
    if any(key in out for key in _MODEL_VISIBLE_DROP_KEYS):
        out = {key: value for key, value in out.items()
               if key not in _MODEL_VISIBLE_DROP_KEYS}
    return out


def tool_call_status(resp: dict) -> str:
    """根据业务事件、ok 标记或传输错误判定工具调用状态。"""
    if 'ok' in resp:
        return 'success' if resp.get('ok') else 'failed'
    ev = str(resp.get('event') or '')
    if ev in ('period_rejected', 'budget_reached', 'root_context_not_found', 'tool_failure'):
        return 'failed'
    return 'success' if ev else 'failed'


def params_digest(args: Any, limit: int | None = 500) -> str:
    """仅保留可向前端展示的查询参数，原始参数不下发。"""
    try:
        text = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(args)
    return text if limit is None else text[:limit]


# ---------- knowledge_search 本地执行 ----------

KNOWLEDGE_RESULT_KEYS = ('doc_id', 'title', 'section', 'doc_type', 'content',
                         *GOVERNANCE_FIELDS, 'governance_warning')

# retired 文档声明替代条目时附加确定性提示，但不将其从检索结果过滤。
RETIRED_GOVERNANCE_WARNING = ('本文 status=retired，是历史知识、已被 {superseded_by} 取代：'
                              '可用于解释当时为什么这么做，但不能单独作为当前判断的依据；'
                              '涉及当前判断时请核对 {superseded_by}。')


def governance_warning(result: dict) -> str | None:
    """唯一一条确定性治理行为：status=retired 且有 superseded_by ⇒ 生成提示，其余一律 None。

    自由文本 scope 不在此触发任何拒绝，也不建按 status 分支的状态机。
    """
    if result.get('status') == 'retired' and result.get('superseded_by'):
        return RETIRED_GOVERNANCE_WARNING.format(superseded_by=result['superseded_by'])
    return None


KNOWLEDGE_DELIVERY_MODE = 'document'


def knowledge_search_response(retriever: Any, args: Any,
                              delivery_mode: str = 'section') -> dict:
    """执行本地知识检索，不占业务查询预算。

    section 模式返回节级结果，document 模式返回固定 Top2 篇全文。
    模型只接收 KNOWLEDGE_RESULT_KEYS 内的字段；全文结果的 section 为 None。
    文档排名、分数与候选块信息不进入模型上下文。"""
    if delivery_mode not in ('section', 'document'):
        raise ValueError(
            f'delivery_mode={delivery_mode!r} 非法（只允许 section|document）')
    if retriever is None:
        return {'ok': False, 'error_code': 'knowledge_unavailable',
                'detail': '知识检索未接线（本图未配置 KnowledgeRetriever；纯数据问题不受影响）'}
    if not isinstance(args, dict):
        return {'ok': False, 'error_code': 'knowledge_invalid_params',
                'detail': 'knowledge_search 参数须为对象（query 必填；doc_type 可省略）'}
    # 模型可见参数合同在本模块单点预校验（retriever 的 ValueError 同码兜底，第二道防线）：
    # 合同镜像 knowledge_search schema（query 非空 str；doc_type 只允许 guide|case 或
    # 省略）。
    query = args.get('query')
    if not isinstance(query, str) or not query.strip():
        return {'ok': False, 'error_code': 'knowledge_invalid_params',
                'detail': 'query 须为非空字符串（检索词）'}
    doc_type = args.get('doc_type')
    if doc_type is not None and doc_type not in DOC_TYPES:
        return {'ok': False, 'error_code': 'knowledge_invalid_params',
                'detail': 'doc_type=%r 非法（只允许 guide|case，或省略）' % (doc_type,)}
    try:
        if delivery_mode == 'document':
            out = retriever.search_documents(query.strip(), doc_type=doc_type)
        else:
            out = retriever.search(query=query.strip(), doc_type=doc_type)
    except ValueError as exc:                  # 模型可见合同校验（retriever 合同）
        return {'ok': False, 'error_code': 'knowledge_invalid_params', 'detail': str(exc)}
    except Exception as exc:                   # noqa: BLE001  索引缺失/存储损坏等底层异常
        return {'ok': False, 'error_code': 'knowledge_unavailable',
                'detail': '%s: %s' % (type(exc).__name__, exc)}
    if delivery_mode == 'document':
        raw_rows = []
        for r in (out.get('documents') or []):
            if not isinstance(r, dict):
                continue
            raw_rows.append({
                'doc_id': r.get('doc_id'),
                'title': r.get('title'),
                'section': None,               # 整篇交付无单一节（见 docstring，不造合成节名）
                'doc_type': r.get('doc_type'),
                'content': r.get('content'),
                **{f: r.get(f) for f in GOVERNANCE_FIELDS},
            })
    else:
        raw_rows = [r for r in (out.get('results') or []) if isinstance(r, dict)]
    results = []
    for r in raw_rows:
        row = {k: r.get(k) for k in KNOWLEDGE_RESULT_KEYS}
        row['governance_warning'] = governance_warning(row)   # 工具层确定性交付，不靠正文节召回
        results.append(row)
    return {'ok': True, 'query': out.get('query'), 'results': results}
