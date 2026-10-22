# -*- coding: utf-8 -*-
"""一次调查的本地状态：运行 ID、预算镜像、工具客户端、知识检索器、事件与步骤。

预算判据来自 core.root_contract，实际计数由服务端 root_budget 回显校准。
RunContext 通过 SDK 上下文传给工具，每次运行独立记账。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.root_contract import (BUDGET_LIMITS, budget_gate_decision,
                                            budget_reject_response, local_budget_view,
                                            params_digest, tool_call_status)
from core.tool_client import FAMILY_BY_TOOL, MCP_QUERY_TOOLS, ToolServerClient

# 当前 Root Agent 只使用 queries 预算；MCP 查询与 oagent_query 共用该额度。
BUDGET_FAMILIES = ('queries',)

# 服务端 root_budget 回显键 → 本地族（只标现役族；BUDGET_FAMILIES 之外的键保留在
# last_server_budget 供排查，不进本地镜像）。
_SERVER_COUNT_KEYS = (('queries_attempted', 'queries'), ('evidence_calls', 'evidence'))

# 对外预算只保留可用工具族；完整服务端回显留在 last_server_budget 供对账。
_INACTIVE_SERVER_PREFIXES = tuple(f for f in ('evidence', 'specialist')
                                  if f not in BUDGET_FAMILIES)


def _window_key(win: Any) -> str | None:
    """响应回显的日期窗口块 → 窗口身份串 `start~end`（Data 原样起止，非编造）；无日期=None。

    与 `display_intent._window_period_id` / 前端 `periodLabel` 同一写法（目录外原样显示
    ＝真实日期区间），因此窗口品类合计快照沿用既有 `base_period_id`/`target_period_id` 字段位
    即可，不新建字段、不伪造目录周期 id。
    """
    if not isinstance(win, dict):
        return None
    start, end = win.get('start_date'), win.get('end_date')
    if not start and not end:
        return None
    return '%s~%s' % (start, end)


@dataclass
class RunContext:
    """SDK 侧的 run 依赖与可观测状态"""

    run_id: str = ''
    client: ToolServerClient | None = None
    retriever: Any | None = None
    # MCP 会话绑定本 run_id；会话缺失时明确返回 mcp_unavailable，不改走其它查询入口。
    mcp: Any | None = None
    budget: dict = field(default_factory=lambda: {f: 0 for f in BUDGET_FAMILIES})
    events: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    last_server_budget: dict = field(default_factory=dict)
    # 无权威预算回显时回滚本次暂记并记录未对账失败，不推定服务端已消耗或未消耗。
    unreconciled_failures: int = 0
    truncation: dict | None = None
    step_seq: int = 0
    # 原生 MCP 工具的调用起点（`tool_input_guardrails` 记录）→ 输出钩子读取，用于算出
    # `record_step` 的 elapsed_ms；键＝SDK 的 tool_call_id。与 FunctionTool 链共用同一记账口径
    # （都只产出一条 `record_step`），不是第二套步骤机制。
    mcp_tool_started_at: dict = field(default_factory=dict)
    # 知识循环收敛状态：knowledge_search 不占业务查询预算，模型侧唯一硬边界是轮次上限；
    # 这里只记「本 run 检索是否还在产生新内容」，供 tools 层注入注记与机械收口（闸-拒绝同形）。
    knowledge_seen_content: set = field(default_factory=set)    # 已返回过的 (doc_id, section, 正文内容摘要)
    knowledge_queries: set = field(default_factory=set)         # 已执行过的 (query, doc_type)
    knowledge_no_new_streak: int = 0                            # 连续「零新增内容」的执行次数
    knowledge_closed: bool = False                              # 收口后所有 knowledge_search 拒绝执行
    # 本 run 实际检索返回的文档（doc_id → {doc_id,title,doc_type}，按首次返回顺序、doc_id 去重）。
    # 只供 `run_finished.knowledge_sources` 展示投影：不参与 Evidence Guard / 数值核验，不存正文。
    knowledge_sources: dict = field(default_factory=dict)

    # 品类合计的运行期快照（**只存品类聚合需要的紧凑数据，不保存整份 Tool response**）。
    # 用途＝模型自己不产 display_blocks（或产了但没有任何 group_totals）时，服务层仍可用
    # Data 已算好的 `period_diff_set.summary.category_totals`（diff 路径）或由基期/对比期
    # `period_values` 紧凑值配对（value 路径）兜底出品类合计展示。
    # 每项由 `note_category_totals` / `note_period_values` 登记（同键幂等覆盖），`kind` 区分
    # 来源：`'diff'`＝Data 已算好的 category_totals；`'values'`＝按 (period_id, metric) 的
    # 紧凑周期值，供兜底装配时配对合成（本侧不保存整份响应）。
    category_total_snapshots: list = field(default_factory=list)

    # 本轮**实际执行过的数据查询**的紧凑范围台账（实际调查对象/比较基准的可信来源）。
    # 每次成功查询由 `note_query_scope` 登记一条：op / 指标 / 周期 / 请求显式 product_ids
    # （或 cohort 的 scope）。它与 `entry_scope`（入口**可查询范围**）是两回事——目录池 60
    # 不代表本轮已调查 60：调查对象是这里登记的实际请求成员，比较基准是 set_compare 那次的
    # 集合。只存紧凑字段（不保存响应体），不参与预算/证据登记。
    query_scopes: list = field(default_factory=list)

    # ---- 实际查询范围台账（不参与任何业务判定与预算） ----

    def note_query_scope(self, query: Any, resp: Any) -> None:
        """登记一次**成功**数据查询的实际请求范围（失败/拒绝/不合规一律跳过）。

        只存紧凑字段：`op` / `metrics` / `period_ids` / `product_ids`（请求显式集合）/
        `scope`（cohort 类筛选范围）/ `scope_n`（cohort 的**权威分母回显**）。**只登记真实成功的
        查询（必须由共享 `tool_call_status` 判为 success，且带数据事件）——被闸门拒绝或传输失败
        不构成"已调查"。同一
        (op, 指标集, 周期集, 成员集, scope.kind, scope_n) 幂等覆盖。取值全部来自**请求或服务端
        权威回显的响应字段**，不从读数值反推成员；value/set_compare 的相对周期缺少请求 ID 时，
        使用成功响应中的 `period.period_id`。
        """
        if not isinstance(query, dict) or not isinstance(resp, dict):
            return
        op = str(query.get('op') or '')
        if (not op or tool_call_status(resp) != 'success'
                or not str(resp.get('event') or '')):
            return                      # 无 op、失败/拒绝或未执行
        metrics = [str(m) for m in (query.get('metrics') or []) if str(m or '')]
        metric = str(query.get('metric') or '')
        if not metrics and metric:
            metrics = [metric]
        period_ids = [str(x) for x in (query.get('period_ids') or []) if str(x or '')]
        if not period_ids:
            for key in ('period_id', 'base_period_id', 'target_period_id'):
                v = str(query.get(key) or '')
                if v and v not in period_ids:
                    period_ids.append(v)
        if not period_ids and op in ('value', 'set_compare'):
            period = resp.get('period') if isinstance(resp.get('period'), dict) else {}
            resolved_period_id = str(period.get('period_id') or '')
            if resolved_period_id:
                period_ids.append(resolved_period_id)
        product_ids = sorted({str(p) for p in (query.get('product_ids') or []) if str(p or '')})
        scope = query.get('scope') if isinstance(query.get('scope'), dict) else None
        scope_kind = str(scope.get('kind') or '') if scope else ''
        scope_ids = sorted({str(p) for p in (scope.get('product_ids') or []) if str(p or '')}) \
            if scope else []
        # `value` 用共享 `scope` 时，请求顶层没有 `product_ids`。实际调查成员优先取服务端
        # 权威回显 `scope_resolution.members`（目录序，category/benchmark_all 都解析到位）；
        # 该字段不可得时回落到请求里 `scope.product_ids`（显式商品范围的等价来源，替代测试替身）。
        # 不推成 0，也不从读数值反推——只是把范围的权威来源从请求顶层换到响应/scope。
        if not product_ids:
            resolution = resp.get('scope_resolution')
            if isinstance(resolution, dict) and isinstance(resolution.get('members'), list):
                product_ids = sorted({str(p) for p in resolution['members'] if str(p or '')})
            elif scope_ids:
                product_ids = list(scope_ids)
        # `scope.kind=category` 的请求里没有显式 `product_ids`（成员由服务端从可信目录
        # 解析），页面筛选范围要能看到实际成员——用服务端权威回显的 `scope_resolution.members`
        # 补齐（目录序），不推算、不从读数反推。`benchmark_all` 仍留空（那是范围池，
        # 分母由 `scope_count` 给出，不是显式成员集）。
        if scope_kind == 'category' and not scope_ids:
            resolution = resp.get('scope_resolution')
            if isinstance(resolution, dict) and isinstance(resolution.get('members'), list):
                scope_ids = sorted({str(p) for p in resolution['members'] if str(p or '')})
        # 全池筛选分母取服务端 scope_count，不能把请求中没有商品列表解释为范围为空。
        scope_n = None
        if op == 'cohort_filter':
            sc = resp.get('scope_count')
            if isinstance(sc, int) and not isinstance(sc, bool):
                scope_n = sc
        entry = {'op': op, 'metrics': metrics, 'period_ids': period_ids,
                 'product_ids': product_ids, 'scope_kind': scope_kind,
                 'scope_product_ids': scope_ids, 'scope_n': scope_n}
        for i, existing in enumerate(self.query_scopes):
            if (isinstance(existing, dict) and existing.get('op') == op
                    and existing.get('metrics') == metrics
                    and existing.get('period_ids') == period_ids
                    and existing.get('product_ids') == product_ids
                    and existing.get('scope_kind') == scope_kind
                    and existing.get('scope_product_ids') == scope_ids
                    and existing.get('scope_n') == scope_n):
                self.query_scopes[i] = entry
                return
        self.query_scopes.append(entry)

    # ---- 品类合计快照（展示兜底的数据来源；不参与任何业务判定与预算） ----

    def note_category_totals(self, query: Any, resp: Any) -> None:
        """登记成功跨期查询的品类汇总，供展示层读取，不复算合计。

        成员来自服务端 scope_resolution，时间来自响应中的周期或真实日期窗口。
        多指标分别登记，指标包含在快照键中，避免不同口径混为一笔。"""
        if not isinstance(query, dict) or not isinstance(resp, dict):
            return
        event = str(resp.get('event') or '')
        if query.get('op') != 'diff' or event not in ('period_diff_set', 'period_window_diff_set'):
            return
        product_ids = query.get('product_ids')
        if not isinstance(product_ids, list) or len(product_ids) < 2:
            resolution = resp.get('scope_resolution')
            members = resolution.get('members') if isinstance(resolution, dict) else None
            if not (isinstance(members, list) and len(members) >= 2):
                return
            product_ids = list(members)
        by_metric = resp.get('by_metric')
        if isinstance(by_metric, dict) and by_metric:
            for metric, block in by_metric.items():
                if not isinstance(block, dict):
                    continue
                self._note_diff_category_total(query, resp, product_ids, str(metric), block)
            return
        summary = resp.get('summary')
        if not isinstance(summary, dict) or not isinstance(summary.get('category_totals'), dict):
            return                      # 只在 Data 确实给了品类合计时登记
        self._note_diff_category_total(query, resp, product_ids,
                                       str(query.get('metric') or resp.get('metric') or ''), resp)

    def _note_diff_category_total(self, query: dict, resp: dict, product_ids: list,
                                  metric: str, block: dict) -> None:
        """单个 diff 指标块的品类合计快照登记（单指标与多指标共用同一实现）。"""
        summary = block.get('summary') if isinstance(block.get('summary'), dict) else resp.get('summary')
        if not isinstance(summary, dict) or not isinstance(summary.get('category_totals'), dict):
            return
        rows = [r for r in (block.get('results') or []) if isinstance(r, dict)]
        event = str(resp.get('event') or '')
        if event == 'period_window_diff_set':
            base = _window_key(resp.get('base_window'))
            target = _window_key(resp.get('target_window'))
        else:
            base = query.get('base_period_id') or next(
                (r.get('base_period_id') for r in rows if r.get('base_period_id')), None)
            target = query.get('target_period_id') or next(
                (r.get('target_period_id') for r in rows if r.get('target_period_id')), None)
        if not base or not target or not metric:
            return
        # `period_diff_set` / `period_window_diff_set` 顶层不带 unit（在逐商品 diff 行上）：
        # 单位一致时取该值，不一致给 None
        units = {row['unit'] for row in rows if row.get('unit')}
        unit = next(iter(units)) if len(units) == 1 else None
        snapshot = {
            'kind': 'diff',
            'product_ids': sorted(str(p) for p in product_ids),
            'metric': metric,
            'base_period_id': str(base),
            'target_period_id': str(target),
            'unit': unit,
            'category_totals': summary['category_totals'],
        }
        key = (snapshot['product_ids'], snapshot['metric'],
               snapshot['base_period_id'], snapshot['target_period_id'])
        # 同（商品集合, 指标, 周期对）幂等覆盖：缓存命中/重复查询不重复登记同一条
        for i, existing in enumerate(self.category_total_snapshots):
            if (isinstance(existing, dict) and existing.get('kind') == 'diff'
                    and (existing.get('product_ids'), existing.get('metric'),
                         existing.get('base_period_id'),
                         existing.get('target_period_id')) == key):
                self.category_total_snapshots[i] = snapshot
                return
        self.category_total_snapshots.append(snapshot)

    def note_period_values(self, query: Any, resp: Any) -> None:
        """登记一次成功多商品 value 查询的**紧凑周期值快照**（value 路径；不合规静默跳过）。

        value 查询是单周期的——单次响应不足以构成「品类两期对比」，只按 (period_id, metric,
        商品集合) 登记 `{product_id: {value, state}}`＋排序后的 `product_ids`；兜底装配时只把
        商品集合**完全一致**的基期/对比期快照配对，用 Data 的 `category_totals_from_value_pairs`
        合成等价品类合计（覆盖 Q3 的 catalog → value×2 → states → knowledge 路径）。
        幂等键＝(period_id, metric, 商品集合)：同集合重复查询覆盖，不同集合各自保留。
        /以外指标不登记（品类合计对可加总指标才有意义）。"""
        if not isinstance(query, dict) or not isinstance(resp, dict):
            return
        if query.get('op') != 'value' or resp.get('event') != 'period_values':
            return
        period = resp.get('period') if isinstance(resp.get('period'), dict) else {}
        period_id = str(period.get('period_id') or '')
        if not period_id:
            return
        requested = query.get('product_ids')
        if not isinstance(requested, list) or len(requested) < 2:
            return
        by_metric: dict[str, dict] = {}
        units: dict[str, set] = {}
        for row in (resp.get('results') or []):
            if not isinstance(row, dict):
                continue
            metric = str(row.get('metric') or '')
            if metric not in ('M1', 'M2'):
                continue
            product_id = str(row.get('product_id') or '')
            if not product_id:
                continue
            value = row.get('value')
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                value = None
            by_metric.setdefault(metric, {})[product_id] = {
                'value': value, 'state': str(row.get('state') or 'unknown')}
            if row.get('unit'):
                units.setdefault(metric, set()).add(str(row['unit']))
        for metric, rows_by_pid in by_metric.items():
            if len(rows_by_pid) < 2:
                continue
            metric_units = units.get(metric, set())
            snapshot = {'kind': 'values', 'period_id': period_id, 'metric': metric,
                        'unit': (next(iter(metric_units)) if len(metric_units) == 1 else None),
                        'product_ids': sorted(rows_by_pid),
                        'rows': rows_by_pid}
            for i, existing in enumerate(self.category_total_snapshots):
                if (isinstance(existing, dict) and existing.get('kind') == 'values'
                        and existing.get('period_id') == period_id
                        and existing.get('metric') == metric
                        and existing.get('product_ids') == snapshot['product_ids']):
                    self.category_total_snapshots[i] = snapshot
                    return
            self.category_total_snapshots.append(snapshot)

    def note_period_object_states(self, query: Any, resp: Any) -> None:
        """登记一次多商品 states 查询的**紧凑身份状态快照**（value 路径的等值门禁素材）。

        `period_values` 本身不含跨期身份判定（record_conflict/period_missing 已在其行状态里），
        而 diff 路径会把「身份跨期不一致」挡在可比集合外——value 配对兜底要用同一门禁，就得
        从 `period_object_states` 取每商品 `identity.state`（同 run 多条 states 查询合并登记）。
        只存 `{product_id: identity_state}`，不保存整份响应。"""
        if not isinstance(query, dict) or not isinstance(resp, dict):
            return
        if query.get('op') != 'states' or resp.get('event') != 'period_object_states':
            return
        rows: dict = {}
        for obj in (resp.get('objects') or []):
            if not isinstance(obj, dict):
                continue
            pid = str(obj.get('product_id') or '')
            if not pid:
                continue
            identity = obj.get('identity') if isinstance(obj.get('identity'), dict) else {}
            rows[pid] = {'identity_state': str(identity.get('state') or '')}
        if not rows:
            return
        snapshot = {'kind': 'states', 'rows': rows}
        for existing in self.category_total_snapshots:
            if isinstance(existing, dict) and existing.get('kind') == 'states':
                existing['rows'].update(rows)
                return
        self.category_total_snapshots.append(snapshot)

    # ---- 预算：先查后扣 ----

    def gate(self, tool_name: str) -> dict:
        """预算闸（机械判据，非 prompt 自觉）。不占预算的工具返回 allowed=True。"""
        family = FAMILY_BY_TOOL.get(tool_name)
        if family is None:
            return {'allowed': True, 'code': '', 'detail': ''}
        decision = budget_gate_decision(family, self.budget)
        if family == 'queries' and not decision['allowed']:
            decision['detail'] = '根任务周期查询有效执行次数已达上限 %d' % BUDGET_LIMITS['queries']
        return decision

    def budget_reject(self, tool_name: str, detail: str) -> dict:
        """生成额度拒绝响应，仅展示当前查询族预算。"""
        response = budget_reject_response(FAMILY_BY_TOOL[tool_name], detail, self.budget,
                                          BUDGET_FAMILIES)
        if tool_name == 'oagent_query':
            response['root_budget']['note'] = (
                '有效查询额度只计实际执行的数据查询；执行前拒绝与缓存命中不计，unknown/no-data 仍计')
        return response

    def charge(self, tool_name: str) -> None:
        """发出业务请求时**乐观暂记**本地额度；收到 root_budget 后以服务端权威读数校准。

        拿不到权威回显的故障由 `note_unreconciled_failure` 回滚这次暂记（不认定已消耗）。"""
        family = FAMILY_BY_TOOL.get(tool_name)
        if family is None:
            return
        self.budget[family] = int(self.budget.get(family) or 0) + 1

    def release_charge(self, tool_name: str) -> None:
        """回滚一次本地暂记额度（只配合 `note_unreconciled_failure`；下限 0，不回滚成负）。"""
        family = FAMILY_BY_TOOL.get(tool_name)
        if family is None:
            return
        self.budget[family] = max(0, int(self.budget.get(family) or 0) - 1)

    def note_unreconciled_failure(self, tool_name: str) -> None:
        """**无服务端权威回显**的故障（传输异常 / 适配层故障）：不认定已消耗有效查询额度。

        `charge` 在发请求前乐观暂记，只有拿到 `root_budget` 才由 `note_server_budget` 校准；
        拿不到回显时服务端是否真的执行无从得知。按现行判据不得直接认定为已消耗，故回滚该次
        暂记并登记不确定次数。回滚是安全的：若服务端其实已执行，下一次带 `root_budget` 的
        响应会以权威读数整体校准本地镜像（可能把本地拉高），不静默少计。"""
        self.release_charge(tool_name)
        self.unreconciled_failures += 1

    def budget_view(self) -> dict:
        """返回当前查询族的本地预算视图。"""
        view = local_budget_view(self.budget, BUDGET_FAMILIES)
        view['note'] = (
            'SDK 本地镜像按有效查询语义计数；Tool Server 返回 root_budget 时同步其权威计数')
        return view

    def note_server_budget(self, resp: Any) -> bool:
        """登记服务端回显并校准现役族本地镜像；返回**本次是否拿到服务端权威回显**。

        非现役族的回显只留在 `last_server_budget`。返回 False（`root_budget` 缺失＝传输/
        适配层故障）时本方法不改本地镜像，由调用方按 `note_unreconciled_failure` 处理那次
        乐观暂记——两条查询通道（FunctionTool 链 / 原生 MCP 接入点）共用这一判定。"""
        if isinstance(resp, dict) and resp.get('root_budget'):
            self.last_server_budget = dict(resp['root_budget'])
            for server_key, family in _SERVER_COUNT_KEYS:
                if family in BUDGET_FAMILIES and server_key in self.last_server_budget:
                    self.budget[family] = int(self.last_server_budget[server_key] or 0)
            return True
        return False

    # ---- 事件与步骤时间线 ----

    def new_step_id(self, prefix: str = 't') -> str:
        """步骤 ID 生成方式与 对照实现"""
        self.step_seq += 1
        return '%s%d' % (prefix, self.step_seq)

    def record_step(self, *, step_id: str, kind: str, tool: str, args: Any,
                    status: str, elapsed_ms: int, result_code: str) -> None:
        """一次工具/闸门步骤 → 两条事件 + 一条步骤（形状=root_loop 下发面，
        服务层 `_wire_step_events` 可直接消费）。

        oagent_query 参数摘要不截断（`limit=None`）：全池查询的 product_ids 超过 500 字，
        截断 JSON 过滤网解析失败会让页面步骤退化为「已查询」泛化文案、证据失配
        （真机复现，2026-10-11）；其余工具维持 500 字缺省行为，键面仍由
        `wire_contract._filter_params` 白名单单点过滤，不扩下发面。

        四个 MCP 查询工具的参数同样是「商品集合 + 周期 + 指标」形状（全池查询一样会
        超过 500 字），故与 `oagent_query` 同享不截断；调用层从直连换成 MCP 传输，页面步骤
        的可读参数面保持不变。"""
        digest = (params_digest(args, limit=None)
                  if tool == 'oagent_query' or tool in MCP_QUERY_TOOLS
                  else params_digest(args))
        code = str(result_code or '')
        self.events.append({'type': 'step_started', 'step_id': step_id, 'kind': kind,
                           'label': tool, 'params_digest': digest})
        self.events.append({'type': 'step_finished', 'step_id': step_id, 'status': status,
                            'elapsed_ms': int(elapsed_ms), 'result_code': code})
        self.steps.append({'step_id': step_id, 'kind': kind, 'tool': tool,
                           'params_digest': digest, 'result_code': code,
                           'status': status, 'elapsed_ms': int(elapsed_ms)})

    def add_event(self, event: dict) -> None:
        """内部排查件（root_init_ok / truncated / invalid_args 等）：不过 SSE 白名单。"""
        self.events.append(event)

    def budget_final(self) -> dict:
        """对账本地 queries 预算镜像与服务端回显，明确记录不一致或未对账失败。

        无权威回显的暂记已回滚，但不推定服务端一定未消耗；完整回显保留供排查。"""
        local = self.budget_view()
        server = {k: v for k, v in (self.last_server_budget or {}).items()
                  if not str(k).startswith(_INACTIVE_SERVER_PREFIXES)}
        counts = [(k, f) for k, f in _SERVER_COUNT_KEYS if f in BUDGET_FAMILIES]
        mismatch = {k: {'local': local.get(k), 'server': server.get(k)}
                    for k, _ in counts if server and local.get(k) != server.get(k)}
        return {'local': local, 'server': server,
                'limits': {f: BUDGET_LIMITS[f] for f in BUDGET_FAMILIES},
                'counts_match': (None if not server else not mismatch),
                'mismatch': mismatch,
                'unreconciled_failures': self.unreconciled_failures,
                'note': '客户端镜像 vs 服务端权威计数；不一致=显式登记不静默'}
