# -*- coding: utf-8 -*-
"""工具数据服务的 HTTP 客户端与模型可见响应装配。

运行 ID 由客户端注入，传输可替换以支持本地测试。只透传业务参数，凭据不进入响应。"""
from __future__ import annotations

import json
import os
from typing import Callable

import httpx

MCP_QUERY_TOOLS = ('get_metric_values', 'compare_periods', 'compare_product_set',
                   'filter_products')
FAMILY_BY_TOOL = {'oagent_query': 'queries', 'oagent_evidence': 'evidence',
                  'oagent_specialist': 'specialist',
                  **{name: 'queries' for name in MCP_QUERY_TOOLS}}

Transport = Callable[[str, dict], dict]   # (path, body) -> 响应 JSON dict


class ToolHttpError(RuntimeError):
    """传输错误 / 非 200（调用方按显式失败处理，不静默兜底）。"""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def _default_transport(base_url: str) -> Transport:
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
    token = os.environ.get('DIAG_TOOLS_API_KEY') or ''
    headers = {'Authorization': 'Bearer %s' % token} if token else {}

    def _post(path: str, body: dict) -> dict:
        url = base_url.rstrip('/') + path
        try:
            resp = httpx.post(url, json=body, timeout=timeout, headers=headers)
        except httpx.HTTPError as exc:
            raise ToolHttpError('tool_server 传输错误 %s%s: %s' % (base_url, path, type(exc).__name__)) from exc
        if resp.status_code != 200:
            raise ToolHttpError('tool_server HTTP %d %s' % (resp.status_code, path),
                                status=resp.status_code)
        try:
            return resp.json()
        except ValueError as exc:
            raise ToolHttpError('tool_server 响应非合法 JSON %s' % path) from exc

    return _post


class ToolServerClient:
    def __init__(self, base_url: str = 'http://127.0.0.1:8099', transport: Transport | None = None):
        self._post = transport or _default_transport(base_url)

    @staticmethod
    def _with_run_id(body: dict, run_id: str) -> dict:
        out = dict(body)
        out['run_id'] = str(run_id or '')
        return out

    def root_init(self, run_id: str, allowed_periods: list, expected_versions: dict,
                  allowed_product_ids: list | None = None) -> dict:
        body = {'allowed_periods': list(allowed_periods or []),
                'expected_versions': dict(expected_versions or {})}
        # 仅显式选择品类时发送商品范围上界。
        if allowed_product_ids:
            body['allowed_product_ids'] = list(allowed_product_ids)
        return self._post('/oagent_root_init', self._with_run_id(body, run_id))

    def query(self, run_id: str, query_obj: dict) -> dict:
        if not isinstance(query_obj, dict):
            raise ToolHttpError('oagent_query 缺 query 对象（模型参数解析失败时不得发请求）')
        return self._post('/oagent_query', self._with_run_id({'query': query_obj}, run_id))

    def evidence(self, run_id: str, product_id, period_id, metrics=None) -> dict:
        body: dict = {'product_id': product_id, 'period_id': period_id}
        if metrics:
            body['metrics'] = list(metrics)
        return self._post('/oagent_evidence', self._with_run_id(body, run_id))

    def qev_guard(self, run_id: str, refs: list) -> dict:
        return self._post('/oagent_qev_guard', self._with_run_id(
            {'refs': [str(r) for r in (refs or [])]}, run_id))

    def evidence_resolve(self, run_id: str, refs: list) -> dict:
        """ 原始依据回查（只读；服务端 = oagent_ctx.resolve_evidence_refs 同一实现）。

        按 run_id + canonical 引用（pqev:/pev:）回查本根任务**已登记**的原始查询/取证元素；
        未签发/跨任务引用逐项 missing。本调用不发起经营查询、不改变预算与登记面；
        仅供服务层证据详情投影消费，不注册成模型可见工具。"""
        return self._post('/oagent_evidence_resolve', self._with_run_id(
            {'refs': [str(r) for r in (refs or [])]}, run_id))

    def specialist_status(self, run_id: str) -> dict:
        """读取根任务的专项登记记录与预算，供服务层投影使用。"""
        return self._post('/oagent_specialist_status', self._with_run_id({}, run_id))


    def ctx_fetch(self) -> dict:
        """无状态获取目录、周期、共享业务口径与合同版本；失败由调用方明确处理。"""
        return self._post('/oagent_ctx_fetch', {})

    def child_init(self, parent_run_id: str, child_run_id: str, specialist_request: dict,
                   expected_versions: dict) -> dict:
        """绑定可信父子运行 ID 并初始化专项上下文。

        服务端依次核对版本、请求和预算，再解析引用；父子运行 ID 不接受模型覆盖。"""
        return self._post('/oagent_child_init', {
            'parent_run_id': str(parent_run_id or ''),
            'child_run_id': str(child_run_id or ''),
            'specialist_request': specialist_request,
            'expected_versions': dict(expected_versions or {}),
        })

    def specialist_report(self, parent_run_id: str, child_run_id: str, result: dict) -> dict:
        """回传专项结果，由服务端核验发现引用并合并解析后的证据索引。"""
        return self._post('/oagent_specialist_report', {
            'parent_run_id': str(parent_run_id or ''),
            'child_run_id': str(child_run_id or ''),
            'result': result,
        })


def tool_response_text(response: dict) -> str:
    """将工具响应统一投影为模型可见文本，剔除非查询族预算与内部标签。"""
    from core.root_contract import model_visible_response

    return json.dumps(model_visible_response(response), ensure_ascii=False)
