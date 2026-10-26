# -*- coding: utf-8 -*-
"""根任务上下文、预算与证据注册表。

按根 run_id 幂等初始化允许周期和预算；经营查询委托 pctx_query，
本模块负责周期约束、预算扣减、证据签发、短引用解析与只读回查。
兼容取证与专项接口维护独立合同，单 Root Agent 不将其作为模型工具。
只读、无模型或文件 IO；缺失、零分母与不可比保留 structured unknown。"""
import json
import re
import time
from functools import partial

from . import pctx_query as PQ                    # noqa: E402
from . import qctx_period as P                    # noqa: E402
from . import period_view_generator as PVG        # noqa: E402
from . import period_resolver as PR               # noqa: E402  （相对时间→canonical 身份）
from . import operating_agent_brief as OAB        # noqa: E402
from . import oi03_brief as OB                    # noqa: E402

OAGENT_CONTRACT_VERSION = 'oagent_root_v1'
ROOT_QUERY_ATTEMPTS_MAX = OAB.OAGENT_ROOT_BUDGET['period_query_attempts']
ROOT_EVIDENCE_CALLS_MAX = OAB.OAGENT_ROOT_BUDGET['evidence_calls']
ROOT_SPECIALIST_CALLS_MAX = OAB.OAGENT_ROOT_BUDGET['specialist_calls']

FOREIGN_QEV_PREFIXES = ('qev:', 'ev:')     # 跨命名空间引用=显式拒绝（与 pctx 守卫同纪律）

# 引用短 handle（服务端分配；模型只按 [E#] 形式引用，不自行编号）
_CITE_HANDLE_RE = re.compile(r'^\[?(E\d+)\]?$')
CITATION_NOTE = ('引用方式：最终回答引用证据请用短 handle 形式 [E1]（handle→真实证据映射见 '
                 'evidence_handles，由服务端按根任务维护，不要自行编号）；不要手写 pqev: '
                 '长引用')


def _cite_handle(ref):
    """引用是否为短 handle 形式（`E1`/`[E1]`）；是则返回 handle 键，否则 None。

    canonical 引用（pqev:/pev:/qev:/ev: 前缀）不可能匹配 `^\\[?E\\d+\\]?$`，无歧义。
    """
    m = _CITE_HANDLE_RE.match(str(ref or '').strip())
    return m.group(1) if m else None


def _handle_for(st, canonical_ref):
    """canonical 证据引用 → 稳定短 handle（E1、E2…）：根任务内单调分配、不回收不重排；
    同一证据重复返回复用既有 handle（幂等）。只对已登记进根任务注册表的引用调用。"""
    handles = st.setdefault('handle_map', {})
    ref = str(canonical_ref)
    for h, c in handles.items():
        if c == ref:
            return h
    h = 'E%d' % (len(handles) + 1)
    handles[h] = ref
    return h


def _handles_view(st, refs):
    """本响应携带的 handle→canonical 映射（模型可见；引用纪律见 CITATION_NOTE）。"""
    return {_handle_for(st, r): str(r) for r in (refs or [])}


def new_root_state():
    """创建按根 run_id 隔离的进程内状态。

    queries_attempted 只计实际执行的数据查询，缓存命中与执行前拒绝不计，unknown 结果仍计。
    query_invocations 为时间解析台账编号，不参与额度计算。
    查询响应、证据注册表与稳定短引用用于核验和回查；父子绑定防止专项另开根任务预算。"""
    return {'contract_version': OAGENT_CONTRACT_VERSION, 'created_ts': None,
            'allowed_periods': [], 'allowed_product_ids': None,
            'queries_attempted': 0, 'query_invocations': 0,
            'evidence_calls': 0, 'specialist_calls': 0, 'period_count': 0,
            'qev_set': set(), 'evidence_registry': {},
            'query_responses': {}, 'specialist_records': [], 'child_bindings': {},
            'handle_map': {},
            'resolved_windows': [],
            'pctx_run_state': None}


def _budget_view(st):
    """返回服务端完整预算计数，供后台对账；模型可见出口只保留当前可用的查询族。"""
    return {'queries_attempted': st.get('queries_attempted'),
            'queries_attempted_max': ROOT_QUERY_ATTEMPTS_MAX,
            'evidence_calls': st.get('evidence_calls'), 'evidence_calls_max': ROOT_EVIDENCE_CALLS_MAX,
            'specialist_calls': st.get('specialist_calls'), 'specialist_calls_max': ROOT_SPECIALIST_CALLS_MAX,
            'period_count': st.get('period_count'),
            'note': '有效查询额度只计实际执行的数据查询；执行前拒绝与缓存命中不计，unknown/no-data 仍计；'
                    '预算耗尽不再发取数/取证/专项请求'}


def _normalize_root_product_ids(allowed_product_ids):
    """归一化入口商品上界，未知或重复成员返回拒绝；空列表表示不设上界。

    按目录顺序排列成员，保证重复初始化时不受请求顺序影响。"""
    raw = [str(p) for p in (allowed_product_ids or []) if str(p or '')]
    if not raw:
        return [], None
    seen, dups = set(), []
    for pid in raw:
        if pid in seen and pid not in dups:
            dups.append(pid)
        seen.add(pid)
    if dups:
        return None, {'error_code': 'root_product_duplicate',
                      'detail': '允许商品集合重复（不静默去重）: %s' % ','.join(dups)}
    catalog = [str(o['product_id']) for o in P.build_view()['objects']]
    unknown = sorted(set(raw) - set(catalog))
    if unknown:
        return None, {'error_code': 'root_product_unknown',
                      'detail': '允许商品集合含目录外 id: %s' % ','.join(unknown)}
    return [pid for pid in catalog if pid in seen], None


def init_root(run_id, allowed_periods=None, registries=None, expected_versions=None,
              allowed_product_ids=None):
    """初始化/续用根任务（幂等）。`allowed_periods`=本轮允许周期（入口选择带入，模型不可改）。"""
    registries = registries if registries is not None else {}
    rid = str(run_id or '').strip()
    if not rid:
        return {'ok': False, 'error_code': 'root_run_id_missing',
                'detail': '根任务 run_id 缺失：由入口初始化并注入，模型不可生成根任务 ID'}
    ver_err = check_expected_versions(expected_versions)
    if ver_err is not None:
        return dict(ver_err, ok=False, run_id=rid)
    products, product_err = _normalize_root_product_ids(allowed_product_ids)
    st = registries.get(rid)
    if st is None:
        if product_err is not None:
            return dict(product_err, ok=False, run_id=rid)
        st = new_root_state()
        st['created_ts'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        periods = [str(p) for p in (allowed_periods or []) if str(p or '')]
        known = {str(p['period_id']) for p in PVG.PERIODS}
        unknown = sorted(set(periods) - known)
        if unknown:
            return {'ok': False, 'error_code': 'root_period_unknown',
                    'detail': '允许周期含合同周期集外 id: %s' % ','.join(unknown)}
        st['allowed_periods'] = periods
        st['allowed_product_ids'] = products or None
        registries[rid] = st
        return {'ok': True, 'event': 'root_created', 'run_id': rid,
                'allowed_periods': list(st['allowed_periods']),
                'allowed_product_ids': list(st['allowed_product_ids'] or []),
                'root_budget': _budget_view(st),
                'note': '根任务初始化（幂等）：周期守卫依据=本轮入口允许周期；'
                        '商品上界=入口显式选品类时的成员集合（缺省不设上界）'}
    new_periods = sorted(str(p) for p in (allowed_periods or []) if str(p or ''))
    if new_periods and new_periods != sorted(st['allowed_periods']):
        return {'ok': False, 'error_code': 'root_period_mismatch',
                'detail': '同根任务不允许中途改允许周期（已有 %s，新 %s）'
                          % ('/'.join(st['allowed_periods']) or '（空）', '/'.join(new_periods)),
                'run_id': rid}
    if allowed_product_ids:
        if product_err is not None:
            return dict(product_err, ok=False, run_id=rid)
        if sorted(products) != sorted(st.get('allowed_product_ids') or []):
            return {'ok': False, 'error_code': 'root_product_scope_mismatch',
                    'detail': '同根任务不允许中途改商品上界（已有 %s，新 %s）'
                              % ('/'.join(st.get('allowed_product_ids') or []) or '（不设上界）',
                                 '/'.join(products) or '（不设上界）'),
                    'run_id': rid}
    return {'ok': True, 'event': 'root_exists', 'run_id': rid,
            'allowed_periods': list(st['allowed_periods']),
            'allowed_product_ids': list(st.get('allowed_product_ids') or []),
            'root_budget': _budget_view(st),
            'note': '根任务已存在：预算/登记表不变（幂等续用）'}


def _root(st, run_id):
    """取根任务状态；不存在=显式失败（上下文丢失/服务重启后不静默重建预算）。"""
    if st is None:
        return None, {'event': 'period_rejected', 'error_code': 'root_context_not_found',
                      'detail': '根任务上下文不存在（run_id=%r）：可能服务重启或未初始化；'
                                '显式结束该运行，不静默重建预算' % str(run_id or '')}
    return st, None


def _window_direction_error(base_period_id, target_period_id):
    """ 显式周期对方向校验：base 必须早于 target（按目录真实时间序，非 ID 字符串序）。

    返回 ''=通过（含任一 id 不在目录的情形——越界 id 已由 scope 检查先行拒绝）；否则返回
    拒绝理由。base==target 同样拒绝（窗口方向不成立：不早于）。
    """
    bi = PVG.PERIOD_INDEX.get(str(base_period_id))
    ti = PVG.PERIOD_INDEX.get(str(target_period_id))
    if bi is None or ti is None or bi < ti:
        return ''
    base_meta = PVG.period_by_id(base_period_id) or {}
    target_meta = PVG.period_by_id(target_period_id) or {}
    return ('周期窗口方向不成立：base_period_id=%s（目录时间序第 %s 期，%s~%s）不早于 '
            'target_period_id=%s（第 %s 期，%s~%s）。diff/states 的 base 须早于 target；'
            '时间序以目录日期区间/period_index 为准，不以 ID 字符串判断'
            % (base_period_id, bi, base_meta.get('period_start_date'),
               base_meta.get('period_end_date'), target_period_id, ti,
               target_meta.get('period_start_date'), target_meta.get('period_end_date')))


def _product_scope_violation(req, allowed_product_ids):
    """检查显式商品范围是否超出根任务上界，越界时拒绝而非裁剪。

    benchmark_all 和未指定成员的 states 由数据层解析为上界内全池；本守卫不拒绝这些隐式范围。"""
    if not allowed_product_ids:
        return ''
    allowed = {str(p) for p in allowed_product_ids}
    view = P.build_view()
    catalog = {str(o['product_id']) for o in view['objects']}

    def _bad_message(bad):
        return ('本次范围含入口选定品类之外的商品：%s（本轮商品上界=%s；上界由入口选择带入，'
                '不擅自扩大到全池）'
                % (','.join(bad), '/'.join(sorted(allowed)) or '（空）'))

    scope = req.get('scope') if isinstance(req, dict) else None
    if isinstance(scope, dict):
        kind = scope.get('kind')
        if kind == 'product_ids' and isinstance(scope.get('product_ids'), list):
            bad = [str(p) for p in scope['product_ids']
                   if str(p) in catalog and str(p) not in allowed]
            return _bad_message(bad) if bad else ''
        if kind == 'category':
            try:
                members = P.resolve_scope(scope, view=view)['members']
            except P.QueryRejected:
                return ''          # 未知品类/空范围等留给 pctx 的 E 类结构化拒绝
            bad = [pid for pid in members if pid not in allowed]
            return _bad_message(bad) if bad else ''
        return ''
    raw = req.get('product_ids') if isinstance(req, dict) else None
    if isinstance(raw, list):
        bad = [str(p) for p in raw if str(p) in catalog and str(p) not in allowed]
        return _bad_message(bad) if bad else ''
    return ''


def _period_violation(req, allowed_periods, allowed_product_ids=None):
    """检查查询周期是否获准，以及比较基期是否早于目标期。

    双期操作必须提供完整周期对；使用跨全视图默认值时，所有涉及周期均须获准。
    商品显式范围同时受入口登记的上界约束。返回空串表示通过。"""
    if not isinstance(req, dict):
        return '查询不是 JSON 对象'
    op = str(req.get('op') or '')
    allowed = set(allowed_periods or [])
    # 日期窗口请求（window/base_window/target_window）走日/月通道——其范围校验在
    # 数据层按**日级数据实际覆盖区间**执行，不由周期选择器决定可用日期。此处的周期键/窗口
    # 方向分支只对周期形态生效（周期与窗口混给由数据层 period_mode_conflict 拒绝）。
    window_mode = any(k in req for k in PR.EXPLICIT_WINDOW_KEYS)
    for k in ('period_id', 'base_period_id', 'target_period_id'):
        if k in req and req.get(k) not in allowed:
            return ('查询 %s=%r 不在本轮允许周期 %s（周期由入口选择带入，不从题面扩展）'
                    % (k, req.get(k), '/'.join(sorted(allowed)) or '（空）'))
    if op == 'states' and not window_mode:
        has_base = 'base_period_id' in req
        has_target = 'target_period_id' in req
        if has_base != has_target:
            return 'states 显式周期窗口须同时给 base_period_id 和 target_period_id'
        if has_base:
            direction = _window_direction_error(req.get('base_period_id'),
                                                req.get('target_period_id'))
            if direction:
                return direction
        if not has_base and allowed != set(PVG.PERIOD_IDS):
            return ('states 未显式选择周期对时会跨视图周期比较；本轮只允许周期 %s，'
                    '须同时显式给 base_period_id/target_period_id，且两期都须获准'
                    % ('/'.join(sorted(allowed)) or '（空）'))
    if op == 'diff':
        has_base = 'base_period_id' in req
        has_target = 'target_period_id' in req
        if has_base != has_target:
            return ('diff 周期对须同时给 base_period_id/target_period_id 或同时省略：'
                    '只给一侧会静默回退目录默认基/末期，可能落出本轮允许周期，不再放行')
        if not has_base and not window_mode and allowed != set(PVG.PERIOD_IDS):
            return ('diff 缺省会跨视图两期，但本轮只允许周期 %s：必须显式给 base_period_id/'
                    'target_period_id' % ('/'.join(sorted(allowed)) or '（空）'))
        if has_base and not window_mode:
            direction = _window_direction_error(req.get('base_period_id'),
                                                req.get('target_period_id'))
            if direction:
                return direction
    if op == 'cohort_filter' and not window_mode:
        # cohort 双期模式与 diff/states 同纪律——周期对缺一/方向选反不再放行；
        # 单期 period_id ∈ 允许周期已由上方通用循环覆盖。cohort 没有跨视图缺省窗口
        # 行为（数据层要求显式周期键或 time 语义），不设 diff 式「缺省须全周期」分支。
        has_base = 'base_period_id' in req
        has_target = 'target_period_id' in req
        if has_base != has_target:
            return ('cohort_filter 双期条件须同时给 base_period_id/target_period_id：'
                    '只给一侧无法构成期间对，不再放行')
        if has_base:
            direction = _window_direction_error(req.get('base_period_id'),
                                                req.get('target_period_id'))
            if direction:
                return direction
    return _product_scope_violation(req, allowed_product_ids)


def execute_root_query(run_id, payload, pctx_run_state=None, root_state=None):
    """执行根任务经营查询，依次解析时间、检查范围，再委托 pctx_query 取数。

    只有实际执行的查询消耗额度；执行前拒绝和缓存命中不计，unknown 结果仍计。
    resolved_window 随响应回显并写入根任务时间解析台账。"""
    st, err = _root(root_state, run_id)
    if err:
        return err
    rid = str(run_id or '')
    if int(st.get('queries_attempted') or 0) >= ROOT_QUERY_ATTEMPTS_MAX:
        return {'event': 'budget_reached', 'budget_kind': 'root_period_query_attempts',
                'contract_version': OAGENT_CONTRACT_VERSION,
                'detail': '根任务周期查询有效执行次数已达上限 %d（整体调用限制不可绕过）'
                          % ROOT_QUERY_ATTEMPTS_MAX,
                'root_budget': _budget_view(st)}
    st['query_invocations'] = int(st.get('query_invocations') or 0) + 1
    invocation = st['query_invocations']
    req = (payload or {}).get('request') if isinstance(payload, dict) else None
    # 相对时间语义 → canonical period 身份（模型给语义，代码给身份映射）。
    resolution = PR.resolve_request(req, st.get('allowed_periods'))
    if resolution is not None:
        if not resolution['ok']:
            st.setdefault('resolved_windows', []).append(
                {'attempt': invocation, 'query_id': None,
                 'event': 'period_rejected', 'error_code': resolution['error_code'],
                 'window': resolution['window']})
            return {'event': 'period_rejected', 'error_code': resolution['error_code'],
                    'detail': resolution['detail'], 'resolved_window': resolution['window'],
                    'root_budget': _budget_view(st)}
        req = PR.apply_resolution(req, resolution['window'])
        payload = {'contract_version': P.PERIOD_CONTRACT_VERSION, 'request': req}
    violation = _period_violation(req, st.get('allowed_periods'),
                                  st.get('allowed_product_ids'))
    if violation:
        # 区分商品越界与周期越界两类 D 类拒绝（ 合同码 `root_product_scope_invalid`
        # 与既有 `root_period_scope_invalid`）。`_period_violation` 返回的字符串两种都可能承载，
        # 这里按同层商品守卫再判一次（纯函数、确定性），不改造错误体系。
        error_code = ('root_product_scope_invalid'
                      if _product_scope_violation(req, st.get('allowed_product_ids'))
                      else 'root_period_scope_invalid')
        rejected = {'event': 'period_rejected', 'error_code': error_code,
                    'detail': violation, 'root_budget': _budget_view(st)}
        if resolution is not None:
            rejected['resolved_window'] = resolution['window']
        return rejected
    if pctx_run_state is None:
        if st.get('pctx_run_state') is None:
            st['pctx_run_state'] = PQ.new_run_state()
        pctx_run_state = st['pctx_run_state']
    out = PQ.execute_query(payload, run_state=pctx_run_state,
                           allowed_product_ids=st.get('allowed_product_ids'))
    if resolution is not None and isinstance(out, dict):
        # 解析成功的窗口 artifact 随响应回显（进入模型上下文与 run 工件）并登记
        # 根任务台账（缓存命中也回显本次语义的窗口；解析确定性 ⇒ 同窗口同 canonical 身份）。
        out = dict(out)
        out['resolved_window'] = resolution['window']
        st.setdefault('resolved_windows', []).append(
            {'attempt': invocation, 'query_id': out.get('query_id'),
             'event': out.get('event'), 'window': resolution['window']})
    if isinstance(out, dict) and out.get('event') != PQ.REJECTED_EVENT:
        qev = out.get('qev') or []
        st.setdefault('qev_set', set()).update(str(x) for x in qev)
        qid = out.get('query_id')
        if qid:
            # 查询响应原样登记（按引用取出真实证据内容用；不含缓存重复覆盖）
            st.setdefault('query_responses', {})[str(qid)] = dict(out)
        if out.get('event') == 'budget_reached':
            pass  # pctx 层历史预算事件：仅旧独立 /pctx_query 路径可达（current 链 Root 预算先到）；
                  # 底层预算拒绝不计入实际执行查询镜像。
        elif not out.get('cache_hit'):
            # 根预算与 period_count 都只计进入数据查询后的实际执行；结构拒绝在上面的分支排除。
            st['queries_attempted'] = int(st.get('queries_attempted') or 0) + 1
            st['period_count'] = int(st.get('period_count') or 0) + 1
    out = dict(out) if isinstance(out, dict) else out
    if isinstance(out, dict):
        if out.get('qev'):
            # 引用短 handle：响应附 handle→canonical 映射与引用纪律说明（缓存命中=幂等复用）
            out['evidence_handles'] = _handles_view(st, out['qev'])
            out['citation_note'] = CITATION_NOTE
        out['root_budget'] = _budget_view(st)
    return out


def execute_root_evidence(run_id, product_id, period_id, metrics=('M1', 'M2'), root_state=None):
    """根任务单期取证（主 Agent 直接工具）。委托 `pctx_query.single_period_evidence`。"""
    st, err = _root(root_state, run_id)
    if err:
        return dict(err, ok=False)
    rid = str(run_id or '')
    if int(st.get('evidence_calls') or 0) >= ROOT_EVIDENCE_CALLS_MAX:
        return {'ok': False, 'error_code': 'root_evidence_budget_reached',
                'detail': '根任务单期核对调用已达上限 %d 次（整体调用限制）'
                          % ROOT_EVIDENCE_CALLS_MAX,
                'root_budget': _budget_view(st)}
    st['evidence_calls'] = int(st.get('evidence_calls') or 0) + 1
    pid = str(period_id or '')
    if pid not in (st.get('allowed_periods') or []):
        return {'ok': False, 'error_code': 'root_period_scope_invalid',
                'detail': '取证周期 %r 不在本轮允许周期 %s（周期由入口选择带入，不从题面扩展）'
                          % (pid, '/'.join(st.get('allowed_periods') or []) or '（空）'),
                'root_budget': _budget_view(st)}
    out = PQ.single_period_evidence(product_id, period_id, metrics=metrics)
    out = dict(out)
    if out.get('ok') and out.get('evidence_id'):
        st.setdefault('evidence_registry', {})[str(out['evidence_id'])] = out
        # 引用短 handle：取证证据同样分配 handle（缓存/重复调用=幂等复用既有 handle）
        out['evidence_handles'] = _handles_view(st, [out['evidence_id']])
        out['citation_note'] = CITATION_NOTE
    out['root_budget'] = _budget_view(st)
    return out


def validate_root_refs(run_id, refs, root_state=None):
    """仅接受本根任务登记的查询或取证引用。

    短引用通过 handle_map 解析后接受相同核验，不绕过注册表。
    跨命名空间、未知或未签发引用明确拒绝；引用存在不代表自然语言结论已被证明。"""
    st, err = _root(root_state, run_id)
    if err:
        return {'event': 'oagent_qev_guard', 'contract_version': OAGENT_CONTRACT_VERSION,
                'ok': False, 'accepted': [], 'rejected': [str(r) for r in (refs or [])],
                'foreign_namespace': [], 'handle_resolutions': {},
                'guard_error': err.get('error_code'),
                'note': '根任务不存在：全部引用按未核验处理'}
    refs = [str(r) for r in (refs or [])]
    accepted, rejected, foreign = [], [], []
    handle_resolutions = {}
    qev_set = set(st.get('qev_set') or set())
    registry = st.get('evidence_registry') or {}
    for r in refs:
        handle = _cite_handle(r)
        if handle is not None:
            # 短 handle 引用：解析回 canonical 后仍走同一登记表核验（handle 只是指针，不是证据）
            canonical = (st.get('handle_map') or {}).get(handle)
            handle_resolutions[handle] = canonical
            if canonical is not None and (canonical in qev_set or canonical in registry):
                accepted.append(canonical)
            else:
                rejected.append(r)
            continue
        if r.startswith(FOREIGN_QEV_PREFIXES):
            rejected.append(r)
            foreign.append(r)
            continue
        if r.startswith('%s:' % PQ.PERIOD_QEV_PREFIX):
            if r in qev_set:
                accepted.append(r)
            else:
                rejected.append(r)
            continue
        if r.startswith('%s:' % PQ.PERIOD_EVIDENCE_PREFIX):
            if r in registry:
                accepted.append(r)
            else:
                rejected.append(r)
            continue
        rejected.append(r)
    return {'event': 'oagent_qev_guard', 'contract_version': OAGENT_CONTRACT_VERSION,
            'ok': not rejected, 'accepted': accepted, 'rejected': rejected,
            'foreign_namespace': foreign, 'handle_resolutions': handle_resolutions,
            'root_budget': _budget_view(st),
            'note': 'pqev=数值证据（引用∈根任务实际查询返回）；pev=单期取证（引用∈根任务实际'
                    '取证响应登记表，对象/周期即身份键）；[E#]=短 handle（解析回 canonical 后按'
                    '同一登记表核验）；qev:/ev: 属旧命名空间，不能为新周期事实背书；'
                    '引用存在不宣称自然语言结论已被证明'}


def root_status(run_id, root_state=None):
    """根任务预算/登记读数（/ 接线核实用；只读）。"""
    st, err = _root(root_state, run_id)
    if err:
        return dict(err, ok=False)
    return {'ok': True, 'run_id': str(run_id or ''), 'root_budget': _budget_view(st),
            'qev_count': len(st.get('qev_set') or set()),
            'evidence_registry_count': len(st.get('evidence_registry') or {}),
            'allowed_periods': list(st.get('allowed_periods') or []),
            'allowed_product_ids': list(st.get('allowed_product_ids') or [])}


# ===================== ：专项经营调查（Qwen 子工作流）合同 =====================

SPECIALIST_CONTRACT_VERSION = 'oagent_specialist_v1'
SPECIALIST_MAX_ITERATIONS = 3    # 每次专项 Agent 最多 3 次模型请求
SPECIALIST_REQUEST_KEYS = ('sub_question', 'target_objects', 'benchmark_scope',
                           'periods', 'evidence_refs', 'expected_delivery', 'user_question')
COMPLETION_STATES = ('completed', 'partial', 'incomplete')
_SPECIALIST_REQUEST_MAX_LEN = 800


def _raw_text(x):
    """将格式错误条目的原始内容统一转为字符串，避免输出任意嵌套对象。"""
    if isinstance(x, str):
        return x
    try:
        return json.dumps(x, ensure_ascii=False)
    except Exception:
        return str(x)


def validate_specialist_request(req, allowed_periods, catalog_ids):
    """专项委派请求结构校验（确定性；服务端权威，与子工作流 entry_adapter 同规则）。

    必填：sub_question（子问题）、target_objects（目标对象，∈目录）、periods（周期，非空且
    ⊆根任务允许周期）、expected_delivery（预期交付）。可选：benchmark_scope（默认=目录全池）、
    evidence_refs（已有证据引用，仅接受 pqev:/pev: 前缀）、user_question（原用户问题随上下文）。
    允许键之外一律确定性拒绝（与查询「未知键拒绝」同纪律）。
    """
    if not isinstance(req, dict):
        return {'ok': False, 'error_code': 'request_not_json',
                'detail': 'specialist_request 必须是 JSON 对象'}
    unknown = sorted(set(req.keys()) - set(SPECIALIST_REQUEST_KEYS))
    if unknown:
        return {'ok': False, 'error_code': 'unknown_keys',
                'detail': '专项请求含未知键: %s（允许键：%s）'
                          % (','.join(unknown), '/'.join(SPECIALIST_REQUEST_KEYS))}
    sub = str(req.get('sub_question') or '').strip()
    if not sub:
        return {'ok': False, 'error_code': 'missing_sub_question',
                'detail': 'sub_question 必填：具体子问题，供专项助手独立交付'}
    if len(sub) > _SPECIALIST_REQUEST_MAX_LEN:
        return {'ok': False, 'error_code': 'sub_question_too_long',
                'detail': 'sub_question 超过 %d 字上限' % _SPECIALIST_REQUEST_MAX_LEN}
    delivery = str(req.get('expected_delivery') or '').strip()
    if not delivery:
        return {'ok': False, 'error_code': 'missing_expected_delivery',
                'detail': 'expected_delivery 必填：本次委派预期交付什么'}
    targets = [str(x) for x in (req.get('target_objects') or []) if str(x or '')]
    if not targets:
        return {'ok': False, 'error_code': 'missing_target_objects',
                'detail': 'target_objects 必填：目标对象（目录内 product_id 数组）'}
    known = set(catalog_ids or [])
    unknown_obj = sorted(set(targets) - known)
    if unknown_obj:
        return {'ok': False, 'error_code': 'unknown_object',
                'detail': 'target_objects 含目录外对象: %s' % ','.join(unknown_obj)}
    bench = [str(x) for x in (req.get('benchmark_scope') or []) if str(x or '')]
    if bench:
        unknown_b = sorted(set(bench) - known)
        if unknown_b:
            return {'ok': False, 'error_code': 'unknown_object',
                    'detail': 'benchmark_scope 含目录外对象: %s' % ','.join(unknown_b)}
    else:
        bench = list(catalog_ids or [])
    periods = [str(x) for x in (req.get('periods') or []) if str(x or '')]
    if not periods:
        return {'ok': False, 'error_code': 'missing_periods',
                'detail': 'periods 必填：本次子问题查询周期（须 ∈ 根任务允许周期）'}
    allowed = set(allowed_periods or [])
    out_of_scope = sorted(set(periods) - allowed)
    if out_of_scope:
        return {'ok': False, 'error_code': 'period_out_of_scope',
                'detail': 'periods 含根任务允许周期外项: %s（允许 %s）'
                          % (','.join(out_of_scope), '/'.join(sorted(allowed)) or '（空）')}
    refs = [str(x) for x in (req.get('evidence_refs') or []) if str(x or '')]
    bad_refs = [r for r in refs
                if not (r.startswith('pqev:') or r.startswith('pev:'))]
    if bad_refs:
        return {'ok': False, 'error_code': 'invalid_evidence_ref',
                'detail': 'evidence_refs 只接受 pqev:/pev: 引用: %s' % ','.join(bad_refs[:5])}
    return {'ok': True, 'meta': {
        'sub_question': sub, 'target_objects': targets, 'benchmark_scope': bench,
        'periods': periods, 'evidence_refs': refs, 'expected_delivery': delivery,
        'user_question': str(req.get('user_question') or '').strip()}}


def _ref_parts(ref):
    """pqev 引用拆解：`pqev:<query_id>:<key>` → (query_id, key)；非 pqev → (None, None)。"""
    if not str(ref or '').startswith('pqev:'):
        return None, None
    rest = str(ref)[len('pqev:'):]
    qid, _, key = rest.partition(':')
    return (qid or None), (key or None)


def _diff_rows_for_metric(response, metric):
    """按引用指标定位跨期比较行：多指标读取 by_metric，单指标读取顶层 results。"""
    by_metric = response.get('by_metric')
    if isinstance(by_metric, dict):
        block = by_metric.get(metric)
        if not isinstance(block, dict):
            return []
        return block.get('results') or []
    return response.get('results') or []


def _set_compare_group_member(response, pid, note):
    """high/equal/low 分组按商品 ID 字符串返回：按所在数组保留真实分组，不重算中位数。"""
    for group in ('high', 'equal', 'low'):
        for member in (response.get('%s_group' % group) or []):
            if isinstance(member, str):
                if member == pid:
                    return {'product_id': member, 'group_member': True, 'group': group,
                            'metric': response.get('metric'),
                            'metric_name': response.get('metric_name'),
                            'unit': response.get('unit'),
                            'note': note}
            elif isinstance(member, dict) and str(member.get('product_id')) == pid:
                return member
    return None


def _locate_ref_content(response, key):
    """在查询响应原样中按 pctx 引用键定位真实内容元素（确定性；定位不到=缺失）。

    与 pctx_query._result_qev_keys 的键形一一对应：object:/period:/state:/diff:/
    compare:/compare_row:/compare_missing:/object_state:/catalog:/cohort:/
    category_total:（品类汇总集合级引用）/ window: / window_object: / window_diff: /
    window_compare: / window_compare_row: / window_compare_missing: / day_series:（日期窗口）。

    `diff:` / `window_diff:` 两个逐商品分支按**引用里的指标**定位行——多指标形态的行在
    `by_metric.<指标>.results`（顶层无 `results`），单指标形态仍读顶层 `results`。
    """
    if not isinstance(response, dict):
        return None
    if key.startswith('window:'):
        want = key[len('window:'):]
        win = response.get('window') or {}
        if '%s~%s' % (win.get('start_date'), win.get('end_date')) == want:
            return {'time_mode': response.get('time_mode'), 'window': win,
                    'event': response.get('event')}
        base = response.get('base_window') or {}
        target = response.get('target_window') or {}
        for win in (base, target):
            if '%s~%s' % (win.get('start_date'), win.get('end_date')) == want:
                return {'time_mode': response.get('time_mode'), 'window': win,
                        'event': response.get('event')}
        return None
    if key.startswith('window_object:'):
        _, pid, metric = key.split(':', 2)
        for blk in (response.get('results') or []):
            if blk.get('product_id') == pid and blk.get('metric') == metric:
                return blk
        return None
    if key.startswith('window_diff:'):
        _, pid, metric, state = key.split(':', 3)
        for r in _diff_rows_for_metric(response, metric):
            if isinstance(r, dict) and r.get('product_id') == pid \
                    and r.get('metric') == metric and r.get('state') == state:
                return r
        return None
    if key.startswith('window_compare_row:'):
        pid = key[len('window_compare_row:'):]
        for item in (response.get('ranking') or []):
            if isinstance(item, dict) and str(item.get('product_id')) == pid:
                return item
        return _set_compare_group_member(response, pid,
                                         'window_set_compare 分组按商品 ID 字符串返回')
    if key.startswith('window_compare_missing:'):
        pid = key[len('window_compare_missing:'):]
        for item in (response.get('missing_group') or []):
            if isinstance(item, dict) and str(item.get('product_id')) == pid:
                return item
        return None
    if key.startswith('window_compare:'):
        return response
    if key.startswith('day_series:'):
        _, pid, metric = key.split(':', 2)
        for r in (response.get('results') or []):
            if r.get('product_id') == pid and r.get('metric') == metric:
                return r
        return None
    if key.startswith('period:'):
        pid = key[len('period:'):]
        return response.get('period') if (response.get('period') or {}).get('period_id') == pid else None
    if key.startswith('object:'):
        _, pid, per, metric = key.split(':', 3)
        for blk in (response.get('results') or []):
            if blk.get('product_id') == pid and blk.get('period_id') == per \
                    and blk.get('metric') == metric:
                return blk
        return None
    if key.startswith('state:'):
        _, pid, per = key.split(':', 2)
        for st_ in (response.get('object_period_states') or []):
            if st_.get('product_id') == pid and st_.get('period_id') == per:
                return st_
        return None
    if key.startswith('diff:'):
        _, pid, metric, state = key.split(':', 3)
        for r in _diff_rows_for_metric(response, metric):
            if isinstance(r, dict) and r.get('product_id') == pid \
                    and r.get('metric') == metric and r.get('state') == state:
                return r
        return None
    if key.startswith('compare_row:'):
        pid = key[len('compare_row:'):]
        for item in (response.get('ranking') or []):
            if isinstance(item, dict) and str(item.get('product_id')) == pid:
                return item
        # 中位数比较组为商品 ID 字符串数组，按成员 ID 回查。
        return _set_compare_group_member(response, pid,
                                         'set_compare 分组按商品 ID 字符串返回')
    if key.startswith('compare_missing:'):
        pid = key[len('compare_missing:'):]
        for item in (response.get('missing_group') or []):
            if isinstance(item, dict) and str(item.get('product_id')) == pid:
                return item
        return None
    if key.startswith('object_state:'):
        pid = key[len('object_state:'):]
        for o in (response.get('objects') or []):
            if isinstance(o, dict) and str(o.get('product_id')) == pid:
                return o
        return None
    if key.startswith('compare:') or key.startswith('catalog:'):
        return response
    if key.startswith('cohort:'):
        #  集合级引用：一次 cohort 查询的响应整体=一条集合证据（规范 scope/期间/
        # 条件、全部命中与未知成员、计数与 view 版本同源），与 compare:/catalog: 同形
        # 返回响应原样；不为每个成员另发引用（pctx_query._result_qev_keys 同约定）。
        return response
    if key.startswith('%s:' % PQ.AGGREGATE_SELECTED_TOTAL_PREFIX):
        return _locate_category_total(
            response, key[len(PQ.AGGREGATE_SELECTED_TOTAL_PREFIX) + 1:], selected=True)
    if key.startswith('%s:' % PQ.AGGREGATE_TOTAL_PREFIX):
        #  品类汇总集合级引用：回查该条品类汇总本身（Data 已算好的
        # summary.category_totals 条目）＋所在查询的指标与两期身份。回复引用应能回到
        # 汇总数值、单位、两期、选中/可比覆盖与不可比原因分类，而不是错误附着到某个
        # 商品行；成员商品集合=本次查询的 product_ids（等价范围由 coverage 计数披露）。
        return _locate_category_total(response, key[len(PQ.AGGREGATE_TOTAL_PREFIX) + 1:])
    # value `aggregate='sum'` 的单期集合级合计引用（`set_sum:<metric>`）：
    # 回查 `single_period_totals` 条目本身——请求集合、指标、单位、实际时间（周期 ID
    # 或日期窗口起止）、selected/comparable/non_comparable 计数与状态分类、合计值
    # （全无效为 unknown，不显示为 0）。成员商品集合=本次查询的 product_ids，
    # 不按 category+metric 定位、不挂在商品行引用上（独立可定位的集合引用）。
    if key.startswith('set_sum:'):
        spt_all = response.get('single_period_totals')
        if not isinstance(spt_all, dict) or not spt_all:
            return None
        # `single_period_totals` 是 {metric: entry} dict；每个指标独立可回查。
        metric = key[len('set_sum:'):]
        spt = spt_all.get(metric)
        if not isinstance(spt, dict) or spt.get('event') != 'single_period_totals':
            return None
        if spt.get('metric') != metric:
            return None
        period = response.get('period') if isinstance(response.get('period'), dict) else {}
        window = response.get('window') if isinstance(response.get('window'), dict) else {}
        return {
            'aggregate_kind': 'set_sum',
            'metric': spt.get('metric'),
            'metric_name': spt.get('metric_name'),
            'unit': spt.get('unit'),
            'aggregate': spt.get('aggregate'),
            'sum': spt.get('sum'),
            'complete': spt.get('complete'),
            'sum_scope': spt.get('sum_scope'),
            'selected_n': spt.get('selected_n'),
            'comparable_n': spt.get('comparable_n'),
            'non_comparable_n': spt.get('non_comparable_n'),
            'non_comparable_by_state': spt.get('non_comparable_by_state'),
            'product_ids': spt.get('product_ids'),
            'period_id': period.get('period_id'),
            'period_label': period.get('label'),
            'start_date': window.get('start_date') or window.get('period_start_date'),
            'end_date': window.get('end_date') or window.get('period_end_date'),
            'window_identity': (window.get('start_date') or window.get('period_start_date'))
                                and ('%s~%s' % (window.get('start_date') or window.get('period_start_date'),
                                                window.get('end_date') or window.get('period_end_date'))),
            'summary_note': spt.get('note'),
        }
    return None


def _locate_category_total(response, want_category, selected=False):
    """定位品类合计证据及其指标、期间与覆盖信息，不重新计算。

    value 的完整品类与所选子集使用不同引用键；子集贡献度分母指向完整品类合计。
    多指标 diff 的引用包含指标段，单指标 diff 不包含；品类键与签发侧使用同一归一规则。"""
    if not isinstance(response, dict):
        return None
    metric = None
    head, sep, tail = str(want_category).partition(':')
    if sep and head in P.METRICS:
        metric, want_category = head, tail
    if metric is None:
        return _locate_diff_category_total(response, want_category)
    # 多指标 diff 的品类合计在 `by_metric.<metric>.summary.category_totals`；value 品类合计
    # 在 `category_totals.<metric>`。按**响应形状**分流，不靠键形猜（两种形状键形相同）。
    if isinstance(response.get('by_metric'), dict):
        return _locate_diff_category_total(response, want_category, metric=metric)
    totals = response.get('category_totals')
    block = totals.get(metric) if isinstance(totals, dict) else None
    if not isinstance(block, dict):
        return None
    match = None
    for entry in (block.get('categories') or []):
        if isinstance(entry, dict) \
                and PQ.category_total_ref_key(entry.get('category')) == want_category:
            match = entry
            break
    if match is None:
        return None
    full = match.get('full_category_total') if isinstance(match.get('full_category_total'), dict) else None
    if selected:
        source = match
    elif match.get('member_scope') == 'selected_members_only':
        if full is None:
            return None
        source = full
    else:
        source = match
    # 完整品类合计必须使用完整品类的成员数与覆盖标记，不能沿用所选子集的范围元数据。
    entry_scope = ('selected_members_only' if selected else 'full_category')
    if entry_scope == 'full_category':
        entry_member_count = match.get('catalog_member_count')
        entry_selected_n = match.get('catalog_member_count')
        entry_coverage_complete = True
    else:
        entry_member_count = match.get('member_count')
        entry_selected_n = match.get('selected_n')
        entry_coverage_complete = match.get('coverage_complete')
    period = response.get('period') if isinstance(response.get('period'), dict) else {}
    window = response.get('window') if isinstance(response.get('window'), dict) else {}
    contributors = _category_contributors(response, metric, match.get('category'))
    # 贡献/成员行只来自本响应的逐商品行：仅当本次范围＝完整品类（行覆盖全成员）时才代表
    # 完整贡献；子集请求的响应只有选中成员行，不能把子集贡献说成完整贡献。
    contributors_scope = match.get('member_scope')
    if contributors_scope != 'full_category':
        contributors = []
    return {
        'kind': 'category_total',
        'aggregate_kind': (PQ.AGGREGATE_SELECTED_TOTAL_PREFIX if selected
                           else PQ.AGGREGATE_TOTAL_PREFIX),
        'shape': 'value',
        'category': match.get('category'),
        'metric': metric,
        'metric_name': P.METRICS[metric]['name'],
        'unit': P.METRICS[metric]['unit'],
        'group_by': block.get('group_by'),
        'group_by_source': block.get('group_by_source'),
        'catalog_member_count': match.get('catalog_member_count'),
        'member_count': entry_member_count,
        'coverage_complete': entry_coverage_complete,
        'complete': source.get('complete'),
        'sum': source.get('sum'),
        'value': source.get('sum'),
        'selected_n': entry_selected_n,
        'comparable_n': source.get('comparable_n'),
        'non_comparable_n': source.get('non_comparable_n'),
        'non_comparable_by_state': source.get('non_comparable_by_state'),
        'member_scope': entry_scope,
        'full_category_total': full,
        'period': period or None,
        'period_id': period.get('period_id'),
        'period_label': period.get('period_label') or period.get('label'),
        'start_date': window.get('start_date') or window.get('period_start_date'),
        'end_date': window.get('end_date') or window.get('period_end_date'),
        'window': window or None,
        'contributors': contributors,
        'contributors_scope': (contributors_scope or 'selected_members_only'),
        'summary_note': block.get('note'),
        'ref': source.get('ref'),
    }


def _category_contributors(response, metric, category):
    """从同一响应已算好的逐商品贡献中收集该品类成员贡献（纯搬运，不重算百分比）。"""
    out = []
    for row in (response.get('results') or []):
        contribution = row.get('contribution') if isinstance(row, dict) else None
        if not isinstance(contribution, dict):
            continue
        if contribution.get('metric') != metric or contribution.get('category') != category:
            continue
        if contribution.get('share_state') != 'ok':
            continue
        out.append({'product_id': str(row.get('product_id') or ''),
                    'value': contribution.get('numerator'),
                    'share_percent': contribution.get('share_percent')})
    out.sort(key=lambda x: (-(x.get('share_percent') or 0), -(x.get('value') or 0),
                            x.get('product_id') or ''))
    return out


def _locate_diff_category_total(response, want_category, metric=None):
    """按指标形状回查 diff 品类合计。

    未指定 metric 时取顶层 summary.category_totals，否则取 by_metric 对应块。
    合计、变化、覆盖及不可比分类均使用已登记原值。"""
    if metric is None:
        totals = (response.get('summary') or {}).get('category_totals') or {}
        rows = response.get('results') or []
        resp_metric = response.get('metric')
        metric_name = response.get('metric_name')
        unit = response.get('unit')
    else:
        block = (response.get('by_metric') or {}).get(metric) or {}
        totals = (block.get('summary') or {}).get('category_totals') or {}
        rows = block.get('results') or []
        resp_metric = metric
        metric_name = block.get('metric_name')
        unit = block.get('unit')
    if not isinstance(totals, dict):
        return None
    match = None
    for entry in (totals.get('categories') or []):
        if isinstance(entry, dict) \
                and PQ.category_total_ref_key(entry.get('category')) == want_category:
            match = entry
            break
    if match is None:
        return None
    base_period = target_period = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if base_period is None and row.get('base_period_id') and row.get('target_period_id'):
            base_period, target_period = row['base_period_id'], row['target_period_id']
        if metric_name is None and row.get('metric_name') is not None:
            metric_name = row.get('metric_name')
        if unit is None and row.get('unit') is not None:
            unit = row.get('unit')
    return {
        'aggregate_kind': PQ.AGGREGATE_TOTAL_PREFIX,
        'category': match.get('category'),
        'metric': resp_metric,
        'metric_name': metric_name,
        'unit': unit,
        'base_period_id': base_period,
        'target_period_id': target_period,
        # 日期窗口 diff 的品类汇总回查同样给出真实起止日期（周期 diff 为 None）。
        'base_window': response.get('base_window'),
        'target_window': response.get('target_window'),
        'group_by': totals.get('group_by'),
        'sum_scope': totals.get('sum_scope'),
        'coverage': match.get('coverage'),
        'base_sum': match.get('base_sum'),
        'target_sum': match.get('target_sum'),
        'absolute_change': match.get('absolute_change'),
        'percent_change': match.get('percent_change'),
        'summary_note': totals.get('note'),
    }


def resolve_evidence_refs(root_state, refs):
    """仅回查当前根任务已签发的真实证据内容。

    pqev 引用先检查签发集合，再定位登记响应；pev 引用读取取证注册表。
    未登记、跨任务或定位失败的引用返回 missing，不作为事实注入。"""
    st = root_state if isinstance(root_state, dict) else {}
    registry = st.get('evidence_registry') or {}
    responses = st.get('query_responses') or {}
    qev_set = set(st.get('qev_set') or set())
    out = []
    for ref in [str(r) for r in (refs or [])]:
        if ref.startswith('pev:'):
            content = registry.get(ref)
            out.append({'ref': ref, 'status': 'resolved' if content is not None else 'missing',
                        'content': content if content is not None else None,
                        'reason': '' if content is not None else 'pev 引用不在根任务取证登记表'})
            continue
        qid, key = _ref_parts(ref)
        if qid is None:
            out.append({'ref': ref, 'status': 'missing', 'content': None,
                        'reason': '只接受 pqev:/pev: 引用（qev:/ev: 属旧命名空间）'})
            continue
        if ref not in qev_set:
            out.append({'ref': ref, 'status': 'missing', 'content': None,
                        'reason': 'pqev 引用未在根任务登记表签发（可能伪造/跨任务引用），'
                                  '即使能按前缀定位响应也不作为已解析证据'})
            continue
        response = responses.get(qid)
        content = _locate_ref_content(response, key) if response is not None else None
        out.append({'ref': ref, 'status': 'resolved' if content is not None else 'missing',
                    'content': content if content is not None else None,
                    'reason': '' if content is not None
                    else 'pqev 引用未能在根任务查询响应原样中定位到内容'})
    return out


def _data_version():
    return {'view_version': PVG.VIEW_VERSION, 'generator_version': PVG.GENERATOR_VERSION,
            'seed': PVG.DEFAULT_SEED,
            'demo_caliber': PVG.DEMO_CALIBER}


CTX_FETCH_CONTRACT_VERSION = 'oagent_ctx_fetch_v1'


def _choice_period_ids(choice_label, period_ids):
    """周期选择标签→period_id 列表（按当前数据面现算）。

    「两期对比」=目录全部周期（数据面扩周期时自动跟随）；其余选择标签的括号内 period_id
    即表单合同（如「上一周期（period_demo_p1）」），解析后校验 ∈ 当前周期目录，
    目录中不存在=空列表（适配器按 invalid_period_choice 显式拒绝，不回退构建期副本）。
    """
    ids = [str(x) for x in (period_ids or [])]
    if str(choice_label) == OB.PERIOD_CHOICE_BOTH:
        return list(ids)
    m = re.search(r'（(period_[A-Za-z0-9_]+)）', str(choice_label))
    if m and m.group(1) in set(ids):
        return [m.group(1)]
    return []


def ctx_fetch(run_id=None):
    """无状态生成商品目录、周期、共享口径及入口标签映射，不消耗查询预算。"""
    catalog = OAB.entry_catalog()
    period_ids = [p['period_id'] for p in catalog['periods']]
    projection = {
        'all_label': OB.OPTION_ALL,
        'period_choices': {lbl: _choice_period_ids(lbl, period_ids)
                           for lbl in OB.PERIOD_CHOICES},
        'product_options': [p.get('scope_label') or p['product_name']
                            for p in catalog['products']],
        # 品类快捷选项（同一目录 `category` 去重、目录序；标签格式单点＝OB.category_options）。
        # 前端只消费本键，服务端 label→成员映射在同一目录现算，两侧不各存一份品类目录。
        'category_options': OB.category_options(),
    }
    return {'ok': True, 'event': 'ctx_fetched', 'contract_version': CTX_FETCH_CONTRACT_VERSION,
            'catalog': catalog,
            'entry_projection': projection,
            'shared_business_note': OAB.shared_business_note(),
            'versions': OAB.ctx_versions(),
            'run_id': str(run_id or ''),
            'note': '无状态取上下文：不建状态、不计预算；run_id 仅作追踪记录；'
                    '消费方失败即显式拒绝，禁止回退到任何图内副本'}


def check_expected_versions(expected):
    """登记前版本核对。返回 None=通过；否则返回结构化拒绝片段。

    expected 必须是 ctx_fetch 响应 `versions` 的原样回传（非空字典，逐键比对）；
    缺失/为空/任一键不一致 → version_changed（键名沿用 version_changed：语义都是
    「登记所依据的数据面版本与当前不符」，缺版本是其中最严重的形态）。
    """
    if not isinstance(expected, dict) or not expected:
        return {'error_code': 'version_changed',
                'detail': 'expected_versions 缺失或为空：登记前必须经取上下文端点取得版本组'
                          '并原样回传（禁止无版本依据登记）'}
    cur = OAB.ctx_versions()
    diff = sorted(k for k in set(expected) | set(cur) if expected.get(k) != cur.get(k))
    if diff:
        return {'error_code': 'version_changed',
                'detail': '数据面版本已变化（取→登之间换版，拒绝登记）：差异字段 %s；'
                          'expected=%s；current=%s'
                          % (','.join(diff),
                             json.dumps({k: expected.get(k) for k in diff}, ensure_ascii=False),
                             json.dumps({k: cur.get(k) for k in diff}, ensure_ascii=False))}
    return None


def assemble_specialist_task_input(meta, evidence_resolved, allowed_periods):
    """专项助手任务输入组装（服务面；子工作流 child_parse 同文本要素，验证器做一致性检查）。

    任务输入=子问题/目标/benchmark/周期/预期交付/原用户问题 + 已有证据（服务按引用取出的
    真实内容，missing 显式标记）+ 共享经营口径 + 周期查询能力说明（与主 Agent 同文本单源）。
    不排查询路线、不给答案数值、不包含预算额度之外的执行指令。
    """
    lines = ['【专项调查任务】',
             '- 子问题：%s' % meta['sub_question'],
             '- 目标对象：%s' % ','.join(meta['target_objects']),
             '- benchmark 范围：%s' % ','.join(meta['benchmark_scope']),
             '- 周期（限于允许周期 %s）：%s'
             % ('/'.join(allowed_periods or []), '/'.join(meta['periods'])),
             '- 预期交付：%s' % meta['expected_delivery']]
    if meta.get('user_question'):
        lines.append('- 原用户问题：%s' % meta['user_question'])
    lines.append('')
    lines.append('【已有证据（服务按引用取出的真实工具内容，可直接使用；missing 标记不得当作事实）】')
    if evidence_resolved:
        for item in evidence_resolved:
            if item['status'] == 'resolved':
                lines.append('- %s → %s' % (item['ref'],
                                            json.dumps(item['content'], ensure_ascii=False)))
            else:
                lines.append('- %s → 【missing：%s】（不得当作事实；需要时可自行查询）'
                             % (item['ref'], item['reason']))
    else:
        lines.append('- （无）')
    lines.append('')
    lines.append(OAB.shared_business_note())
    lines.append('')
    lines.append(OAB.oagent_period_brief())
    return '\n'.join(lines)


def _specialist_call_summary(st):
    return {'queries_attempted': st.get('queries_attempted'),
            'evidence_calls': st.get('evidence_calls'),
            'specialist_calls': st.get('specialist_calls'),
            'specialist_max_iterations': SPECIALIST_MAX_ITERATIONS,
            'note': '使用量为服务端登记（根任务预算/工具调用留痕），不是模型自述'}


def specialist_init(run_id, child_run_id, specialist_request, root_state=None,
                    expected_versions=None):
    """专项委派初始化（服务端权威检查；/oagent_child_init 与 execute_root_specialist 共用）。

    顺序：根任务存在 → **版本核对** → 请求结构校验（不合法不扣专项
    额度）→ 专项预算（预留 2 次，失败也扣）→ 子执行 ID 绑定（子 run_id 不得另开根任务；
    已绑定其他根任务=显式拒绝）→ 按引用解析真实证据内容。被拒/预算耗尽返回结构化失败
    （root_budget 随附），由主 Agent 在剩余预算内决定。版本核对与成员校验都必须在预算扣减
    之前完成——child_init「失败也扣」，后置会让非法请求烧额度（既有合同）。
    `expected_versions`=取上下文响应 versions 原样回传；缺失/不一致=version_changed 拒绝。
    """
    st, err = _root(root_state, run_id)
    if err:
        return dict(err, ok=False)
    ver_err = check_expected_versions(expected_versions)
    if ver_err is not None:
        return dict(ver_err, ok=False, run_id=str(run_id or ''),
                    root_budget=_budget_view(st))
    checked = validate_specialist_request(specialist_request, st.get('allowed_periods'),
                                          OAB.all_product_ids())
    if not checked['ok']:
        return dict(checked, ok=False, run_id=str(run_id or ''),
                    root_budget=_budget_view(st))
    meta = checked['meta']
    if int(st.get('specialist_calls') or 0) >= ROOT_SPECIALIST_CALLS_MAX:
        return {'ok': False, 'error_code': 'specialist_budget_reached',
                'detail': '根任务专项委派已达上限 %d 次（整体调用限制，失败/重复同样扣减）'
                          % ROOT_SPECIALIST_CALLS_MAX,
                'run_id': str(run_id or ''), 'root_budget': _budget_view(st)}
    st['specialist_calls'] = int(st.get('specialist_calls') or 0) + 1
    delegation_index = int(st.get('specialist_calls') or 0)
    child = str(child_run_id or '').strip() or 'local-child-%s-%d' % (str(run_id or ''), delegation_index)
    bindings = st.setdefault('child_bindings', {})
    old = bindings.get(child)
    if old is not None and str(old.get('parent_run_id')) != str(run_id or ''):
        return {'ok': False, 'error_code': 'child_run_id_bound_to_other_root',
                'detail': '子执行 ID %r 已绑定其他根任务（%s）：子 run_id 不能另开查询状态'
                          % (child, old.get('parent_run_id')),
                'run_id': str(run_id or ''), 'root_budget': _budget_view(st)}
    bindings[child] = {'parent_run_id': str(run_id or ''), 'bound_ts': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                                                                     time.gmtime())}
    evidence_resolved = resolve_evidence_refs(st, meta['evidence_refs'])
    return {'ok': True, 'event': 'specialist_init', 'contract_version': SPECIALIST_CONTRACT_VERSION,
            'parent_run_id': str(run_id or ''), 'child_run_id': child,
            'delegation_index': delegation_index,
            'request_meta': meta, 'evidence_resolved': evidence_resolved,
            'allowed_periods': list(st.get('allowed_periods') or []),
            'data_version': _data_version(),
            'root_budget': _budget_view(st)}


def stub_specialist_runner(task_input, ctx):
    """【替身】专项助手执行替身（本地路径，非真实 Qwen 调用）。"""
    meta = ctx.get('request_meta') or {}
    targets = meta.get('target_objects') or []
    periods = meta.get('periods') or []
    query_fn = ctx.get('query_fn')
    run_id = ctx.get('run_id')
    calls = []
    out = None
    if query_fn is not None and targets and periods:
        out = query_fn({'contract_version': P.PERIOD_CONTRACT_VERSION, 'request': {
            'op': 'value', 'product_ids': targets, 'period_id': periods[0],
            'metrics': ['M1', 'M2']}})
        calls.append({'tool': 'oagent_query', 'event': out.get('event'),
                      'error_code': out.get('error_code'),
                      'ok': out.get('event') == 'period_values'})
    else:
        calls.append({'tool': '(none)', 'event': 'no_query', 'ok': False,
                      'detail': '任务输入缺少目标对象或周期，替身不查询'})
    qev = (out or {}).get('qev') or []
    if qev:
        block = next((b for b in (out or {}).get('results') or [] if b.get('state') == 'ok'), None)
        # 数值发现引用对应的 object 级引用（object:<pid>:<period>:<metric>），period 级引用作身份回退
        obj_ref = next((r for r in qev
                        if block is not None
                        and r == 'pqev:%s:object:%s:%s:%s'
                        % (out.get('query_id'), block.get('product_id'), block.get('period_id'),
                           block.get('metric'))), None)
        cite_ref = obj_ref or (qev[0] if qev else '')
        payload = {
            'findings': [{'text': '目标对象在周期 %s 的 M1/M2 实测值见引用（%s），替身按真实工具返回转述。'
                                  % (periods[0], cite_ref),
                          'evidence_refs': [cite_ref] if cite_ref else []}],
            'uncertainties': [],
            'completion': 'completed',
            'completion_reason': '替身已按真实同源查询返回给出发现',
        }
        if block is not None:
            payload['findings'][0]['text'] = (
                '目标对象 %s 周期 %s 的 %s=%s%s（替身转述真实工具返回，引用见 evidence_refs）。'
                % (block.get('product_id'), block.get('period_id'), block.get('metric'),
                   block.get('value'), block.get('unit') or ''))
            payload['findings'][0]['evidence_refs'] = [cite_ref] if cite_ref else []
    else:
        payload = {
            'findings': [],
            'uncertainties': ['替身查询未取得可引用数值（%s）：需要真实模型在平台侧补查'
                              % ((out or {}).get('error_code') or 'no_qev')],
            'completion': 'incomplete',
            'completion_reason': '替身查询未返回可引用证据（事件=%s，error_code=%s）'
                                 % ((out or {}).get('event'), (out or {}).get('error_code')),
        }
    raw = '```json\n%s\n```' % json.dumps(payload, ensure_ascii=False)
    return {'raw_reply': raw, 'calls': calls, 'stub': True,
            'note': '替身执行（本地验证用，非真实 Qwen 模型调用）'}


def _extract_obj_pure(text):
    """图内 output_adapter `_extract_obj` 的服务端镜像（整段→围栏→首尾花括号三级提取）。"""
    s = str(text or '').strip()
    if not s:
        return None
    try:
        o = json.loads(s)
        return o if isinstance(o, dict) else None
    except Exception:
        pass
    fence = s.find('```json')
    if fence >= 0:
        start = s.find('{', fence)
        end = s.rfind('}')
        if start >= 0 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except Exception:
                return None
    start, end = s.find('{'), s.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            return None
    return None


def _scan_closed_objects(s, pos):
    """逐个提取已闭合的顶层 JSON 对象，正确处理字符串和转义。"""
    objs = []
    n = len(s)
    while pos < n:
        while pos < n and s[pos] in ' \n\r\t,':
            pos += 1
        if pos >= n or s[pos] == ']':
            return objs, True
        i = s.find('{', pos)
        if i < 0:
            return objs, False
        depth = 0
        in_str = False
        esc = False
        j = i
        closed = False
        while j < n:
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        closed = True
                        break
            j += 1
        if not closed:
            return objs, False
        try:
            o = json.loads(s[i:j + 1])
            if isinstance(o, dict):
                objs.append(o)
        except Exception:
            pass
        pos = j + 1
    return objs, False


def _scan_closed_strings(s, pos):
    """逐个提取已闭合的 JSON 字符串。"""
    items = []
    n = len(s)
    while pos < n:
        while pos < n and s[pos] in ' \n\r\t,':
            pos += 1
        if pos >= n or s[pos] == ']':
            return items, True
        i = s.find('"', pos)
        if i < 0:
            return items, False
        j = i + 1
        esc = False
        closed = False
        while j < n:
            c = s[j]
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                closed = True
                break
            j += 1
        if not closed:
            return items, False
        try:
            v = json.loads(s[i:j + 1])
            if isinstance(v, str):
                items.append(v)
        except Exception:
            return items, True
        pos = j + 1
    return items, False


def _salvage_arrays(text):
    """整段解析失败时保留 findings/uncertainties 数组中已闭合的条目，不补全未闭合内容。"""
    t = str(text or '')
    findings, uncertainties, notes = [], [], []
    fi = t.find('"findings"')
    if fi >= 0:
        lb = t.find('[', fi)
        if lb >= 0:
            objs, closed = _scan_closed_objects(t, lb + 1)
            for f in objs:
                ftext = f.get('text')
                refs = f.get('evidence_refs')
                if not isinstance(ftext, str) or not ftext.strip():
                    notes.append({'kind': 'finding', 'index': -1,
                                  'reason': '救援条目 finding.text 缺失或为空',
                                  'raw': _raw_text(f)})
                    continue
                if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
                    notes.append({'kind': 'finding', 'index': -1,
                                  'reason': '救援条目 finding.evidence_refs 非字符串数组',
                                  'raw': _raw_text(f)})
                    continue
                findings.append({'text': ftext, 'evidence_refs': [str(r) for r in refs]})
            if not closed:
                notes.append({'kind': 'truncation', 'index': -1,
                              'reason': '输出截断：findings 数组未闭合，已救援 %d 条完整条目' % len(findings),
                              'raw': _raw_text(t[-120:])})
    ui = t.find('"uncertainties"')
    if ui >= 0:
        lb = t.find('[', ui)
        if lb >= 0:
            strs, closed = _scan_closed_strings(t, lb + 1)
            uncertainties = [u for u in strs if u.strip()]
            if not closed:
                notes.append({'kind': 'truncation', 'index': -1,
                              'reason': '输出截断：uncertainties 数组未闭合，已救援 %d 条' % len(uncertainties),
                              'raw': _raw_text(t[-120:])})
    return findings, uncertainties, notes


def parse_specialist_output(raw_text):
    """解析专项结果，校验发现、未决项与完成状态的字段形状。

    非法条目分别记录，不混入合法发现；原始回复保留在 raw_reply。
    截断时只保留已闭合条目，标为 partial，structured_valid 仍为 False。"""
    text = str(raw_text or '')
    obj = _extract_obj_pure(text)
    if not isinstance(obj, dict):
        salv_f, salv_u, salv_notes = _salvage_arrays(text)
        if salv_f or salv_u:
            # 截断结果只保留已闭合条目，标为 partial，structured_valid 仍为 False。
            return {'structured_valid': False, 'completion': 'partial',
                    'completion_reason': ('输出被截断（模型生成上限），已救援 %d 条 findings、%d 条 uncertainties；'
                                          '未闭合尾条目未计入，缺失部分见本说明' % (len(salv_f), len(salv_u))),
                    'findings': salv_f, 'uncertainties': salv_u,
                    'malformed_entries': salv_notes, 'raw_reply': text}
        return {'structured_valid': False, 'completion': 'incomplete',
                'completion_reason': 'format_invalid（回复不是合法 JSON 对象）',
                'findings': [], 'uncertainties': [], 'malformed_entries': [],
                'raw_reply': text}
    findings, uncertainties, malformed = [], [], []
    raw_findings = obj.get('findings')
    if not isinstance(raw_findings, list):
        malformed.append({'kind': 'findings', 'index': -1,
                          'reason': 'findings 不是数组', 'raw': _raw_text(raw_findings)})
    else:
        for i, f in enumerate(raw_findings):
            if not isinstance(f, dict):
                malformed.append({'kind': 'finding', 'index': i,
                                  'reason': 'finding 不是对象（数组元素类型必须同质）',
                                  'raw': _raw_text(f)})
                continue
            ftext = f.get('text')
            refs = f.get('evidence_refs')
            if not isinstance(ftext, str) or not ftext.strip():
                malformed.append({'kind': 'finding', 'index': i,
                                  'reason': 'finding.text 缺失或为空', 'raw': _raw_text(f)})
                continue
            if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
                malformed.append({'kind': 'finding', 'index': i,
                                  'reason': 'finding.evidence_refs 必须是字符串数组',
                                  'raw': _raw_text(f)})
                continue
            findings.append({'text': ftext, 'evidence_refs': [str(r) for r in refs]})
    raw_unc = obj.get('uncertainties')
    if not isinstance(raw_unc, list):
        malformed.append({'kind': 'uncertainties', 'index': -1,
                          'reason': 'uncertainties 不是数组', 'raw': _raw_text(raw_unc)})
    else:
        for i, u in enumerate(raw_unc):
            if not isinstance(u, str):
                malformed.append({'kind': 'uncertainty', 'index': i,
                                  'reason': 'uncertainty 不是字符串（数组元素类型必须同质）',
                                  'raw': _raw_text(u)})
                continue
            uncertainties.append(u)
    completion = str(obj.get('completion') or '')
    reason = str(obj.get('completion_reason') or '')
    if completion not in COMPLETION_STATES:
        malformed.append({'kind': 'completion', 'index': -1,
                          'reason': 'completion 必须 ∈ %s' % '/'.join(COMPLETION_STATES),
                          'raw': _raw_text(obj.get('completion'))})
        completion = 'incomplete'
        if not reason:
            reason = 'completion 值不合法'
    structured_valid = not malformed
    return {'structured_valid': structured_valid, 'completion': completion,
            'completion_reason': reason, 'findings': findings,
            'uncertainties': uncertainties, 'malformed_entries': malformed,
            'raw_reply': text}


def _register_specialist_record(root_state, record):
    root_state.setdefault('specialist_records', []).append(record)
    return len(root_state['specialist_records'])


def execute_root_specialist(run_id, specialist_request, root_state=None, runner=None,
                            child_run_id=None, pctx_run_state=None):
    """执行注入的专项 runner，解析结果并逐条核验引用，最后登记到根任务。

    默认 runner 为本地替身；调用和预算摘要由服务端附加，不采信模型自报。"""
    init = specialist_init(run_id, child_run_id, specialist_request, root_state=root_state,
                           expected_versions=OAB.ctx_versions())
    if not init['ok']:
        return init
    st = root_state if isinstance(root_state, dict) else {}
    ctx = {'run_id': init['parent_run_id'], 'child_run_id': init['child_run_id'],
           'request_meta': init['request_meta'],
           'task_input': assemble_specialist_task_input(init['request_meta'],
                                                        init['evidence_resolved'],
                                                        init['allowed_periods']),
           'root_state': st,
           'pctx_run_state': pctx_run_state,
           'query_fn': partial(execute_root_query, init['parent_run_id'],
                               root_state=st, pctx_run_state=pctx_run_state),
           'evidence_fn': partial(execute_root_evidence, init['parent_run_id'],
                                  root_state=st),
           'budget_view': _budget_view(st)}
    if runner is None:
        runner = stub_specialist_runner
    try:
        raw = runner(ctx['task_input'], ctx)
    except Exception as e:      # 替身/注入 runner 异常=显式失败，不伪造完整答复
        raw = {'raw_reply': '', 'calls': [{'tool': '(runner)', 'event': 'runner_error',
                                           'ok': False, 'detail': str(e)[:200]}]}
    raw_reply = str((raw or {}).get('raw_reply') or '')
    parsed = parse_specialist_output(raw_reply)
    findings = []
    for f in parsed['findings']:
        refs = f.get('evidence_refs') or []
        if not refs:
            findings.append(dict(f, evidence_status='demoted', demotion_reason='发现未附证据引用'))
            continue
        guard = validate_root_refs(init['parent_run_id'], refs, root_state=st)
        if guard['rejected']:
            reasons = ['引用不在根任务登记表/跨命名空间: %s' % ','.join(guard['rejected'])]
            findings.append(dict(f, evidence_status='demoted', demotion_reason=reasons[0]))
        else:
            # kept 发现附短 handle（引用已登记证据；主 Agent 可用 [E#] 引用子证据）
            findings.append(dict(f, evidence_status='kept', demotion_reason='',
                                 evidence_handles=[_handle_for(st, str(r)) for r in refs]))
    evidence_index = [
        {'ref': item['ref'], 'status': item['status'],
         'content': item['content'],
         'object': (item['content'] or {}).get('product_id'),
         'period': (item['content'] or {}).get('period_id'),
         'metric': (item['content'] or {}).get('metric')}
        for item in init['evidence_resolved']]
    record = {
        'delegation_index': init['delegation_index'],
        'parent_run_id': init['parent_run_id'], 'child_run_id': init['child_run_id'],
        'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'request_summary': {'sub_question': init['request_meta']['sub_question'],
                            'target_objects': init['request_meta']['target_objects'],
                            'periods': init['request_meta']['periods'],
                            'expected_delivery': init['request_meta']['expected_delivery']},
        'findings': findings, 'uncertainties': parsed['uncertainties'],
        'completion': parsed['completion'], 'completion_reason': parsed['completion_reason'],
        'malformed_entries': parsed['malformed_entries'],
        'raw_reply': parsed['raw_reply'],
        'evidence_index': evidence_index,
        'call_summary': _specialist_call_summary(st),
        'runner_calls': list((raw or {}).get('calls') or []),   # 运输层调用留痕（含 runner 异常）
        'root_budget': _budget_view(st),
        'structured_valid': parsed['structured_valid'],
        'transport_ok': True,
        'demo_note': '专项委派记录：发现引用逐条经根任务注册表核验；使用量为服务端登记；'
                     '原始模型回复另存 raw_reply；数据为 Demo 模拟口径',
    }
    _register_specialist_record(st, record)
    return {'ok': True, 'event': 'specialist_result', 'contract_version': SPECIALIST_CONTRACT_VERSION,
            'parent_run_id': init['parent_run_id'], 'child_run_id': init['child_run_id'],
            'delegation_index': init['delegation_index'],
            'findings': findings, 'uncertainties': parsed['uncertainties'],
            'completion': parsed['completion'], 'completion_reason': parsed['completion_reason'],
            'malformed_entries': parsed['malformed_entries'], 'raw_reply': parsed['raw_reply'],
            'evidence_index': evidence_index,
            'call_summary': _specialist_call_summary(st),
            'runner_calls': list((raw or {}).get('calls') or []),
            'root_budget': _budget_view(st),
            'structured_valid': parsed['structured_valid'],
            'transport_ok': True,
            'demo_note': record['demo_note']}


def specialist_report(run_id, child_run_id, result, root_state=None):
    """核对父子任务绑定并登记专项回传结果，逐条核验发现引用。

    保留调用方对原始回复的格式判定，不从清洗后的合法条目反推 structured_valid，
    避免掩盖原始格式错误；损坏的回传结构明确拒绝。"""
    st, err = _root(root_state, run_id)
    if err:
        return dict(err, ok=False)
    child = str(child_run_id or '').strip()
    bindings = st.get('child_bindings') or {}
    bound = bindings.get(child)
    if not child or bound is None or str(bound.get('parent_run_id')) != str(run_id or ''):
        return {'ok': False, 'error_code': 'child_run_id_not_bound_to_root',
                'detail': '子执行 ID %r 未绑定根任务 %r（跨任务回传拒绝）' % (child, str(run_id or '')),
                'run_id': str(run_id or '')}
    if not isinstance(result, dict):
        return {'ok': False, 'error_code': 'result_not_json',
                'detail': '专项回传结果必须是 JSON 对象', 'run_id': str(run_id or '')}
    findings_raw = result.get('findings')
    uncertainties_raw = result.get('uncertainties')
    completion = str(result.get('completion') or '')
    reason = str(result.get('completion_reason') or '')
    malformed = result.get('malformed_entries')
    structured_valid = result.get('structured_valid')
    raw_reply = str(result.get('raw_reply') or '')
    if not isinstance(findings_raw, list) \
            or not all(isinstance(f, dict) and isinstance(f.get('text'), str) and f.get('text')
                       and isinstance(f.get('evidence_refs'), list)
                       and all(isinstance(r_, str) for r_ in f.get('evidence_refs'))
                       for f in findings_raw):
        return {'ok': False, 'error_code': 'result_invalid_findings',
                'detail': '回传 findings 必须是 {text, evidence_refs[]} 对象数组',
                'run_id': str(run_id or '')}
    if not isinstance(uncertainties_raw, list) \
            or not all(isinstance(u, str) for u in uncertainties_raw):
        return {'ok': False, 'error_code': 'result_invalid_uncertainties',
                'detail': '回传 uncertainties 必须是字符串数组', 'run_id': str(run_id or '')}
    if completion not in COMPLETION_STATES:
        return {'ok': False, 'error_code': 'result_invalid_completion',
                'detail': '回传 completion 必须 ∈ %s' % '/'.join(COMPLETION_STATES),
                'run_id': str(run_id or '')}
    if not isinstance(malformed, list) \
            or not all(isinstance(m, dict) for m in malformed):
        return {'ok': False, 'error_code': 'result_invalid_malformed',
                'detail': '回传 malformed_entries 必须是对象数组', 'run_id': str(run_id or '')}
    if not isinstance(structured_valid, bool):
        return {'ok': False, 'error_code': 'result_invalid_structured_valid',
                'detail': '回传 structured_valid 必须是布尔', 'run_id': str(run_id or '')}
    findings = []
    for f in findings_raw:
        refs = f.get('evidence_refs') or []
        if not refs:
            findings.append(dict(f, evidence_status='demoted', demotion_reason='发现未附证据引用'))
            continue
        guard = validate_root_refs(str(run_id or ''), refs, root_state=st)
        if guard['rejected']:
            findings.append(dict(f, evidence_status='demoted',
                                 demotion_reason='引用不在根任务登记表/跨命名空间: %s'
                                                 % ','.join(guard['rejected'])))
        else:
            # kept 发现附短 handle（引用已登记证据；主 Agent 可用 [E#] 引用子证据）
            findings.append(dict(f, evidence_status='kept', demotion_reason='',
                                 evidence_handles=[_handle_for(st, str(r)) for r in refs]))
    # 问题 4：evidence_index 合并——专项内 Qwen 新查询产生的新证据按**最终发现的引用**
    # 从根任务登记表取出真实内容补进索引（只传初始化时已有证据会漏掉专项内新查到的引用；
    # 委派时未传证据时索引仍为空=错误）。服务端权威解析，去重合并。
    merged_refs = []
    seen_refs = set()
    for item in (result.get('evidence_index') or []):
        r_ = str(item.get('ref')) if isinstance(item, dict) and item.get('ref') else ''
        if r_ and r_ not in seen_refs:
            seen_refs.add(r_)
            merged_refs.append(r_)
    for f in findings_raw:
        for r_ in f.get('evidence_refs') or []:
            if r_ not in seen_refs:
                seen_refs.add(r_)
                merged_refs.append(r_)
    evidence_index = resolve_evidence_refs(st, merged_refs)
    record = {
        'delegation_index': int(st.get('specialist_calls') or 0),
        'parent_run_id': str(run_id or ''), 'child_run_id': child,
        'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'request_summary': None,   # 子图回传不带请求摘要；请求侧信息在 child_init 登记
        'findings': findings,
        'uncertainties': list(uncertainties_raw),
        'completion': completion, 'completion_reason': reason,
        'malformed_entries': list(malformed),     # 保留 output_adapter 判定的原始格式问题
        'raw_reply': raw_reply,
        'evidence_index': evidence_index,         # 合并后：初始化已有证据+专项内新发现引用
        'call_summary': _specialist_call_summary(st),
        'root_budget': _budget_view(st),
        'structured_valid': structured_valid,     # 保留 output_adapter 判定，不二次推导
        'transport_ok': True,
        'demo_note': '专项回传登记：发现引用逐条经根任务注册表核验；'
                     '格式判定（malformed/structured_valid）保留子工作流 output_adapter 结果；'
                     '证据索引=服务端按最终发现引用从根任务登记表解析合并；'
                     '使用量为服务端登记；数据为 Demo 模拟口径',
    }
    _register_specialist_record(st, record)
    return {'ok': True, 'event': 'specialist_report_registered',
            'parent_run_id': str(run_id or ''), 'child_run_id': child,
            'delegation_index': record['delegation_index'],
            'findings': findings, 'uncertainties': list(uncertainties_raw),
            'completion': completion, 'completion_reason': reason,
            'malformed_entries': list(malformed), 'structured_valid': structured_valid,
            'raw_reply': raw_reply, 'evidence_index': evidence_index,
            'root_budget': _budget_view(st)}


def specialist_status(run_id, root_state=None):
    """专项委派记录读数（主图投影 /oagent_specialist_status；只读）。"""
    st, err = _root(root_state, run_id)
    if err:
        return dict(err, ok=False)
    return {'ok': True, 'run_id': str(run_id or ''),
            'records': list(st.get('specialist_records') or []),
            'root_budget': _budget_view(st),
            'note': '专项委派记录=服务端登记（回传/本地执行路径），发现引用逐条核验；'
                    '使用量为服务端登记非模型自述'}


__all__ = ['OAGENT_CONTRACT_VERSION', 'ROOT_QUERY_ATTEMPTS_MAX', 'ROOT_EVIDENCE_CALLS_MAX',
           'ROOT_SPECIALIST_CALLS_MAX', 'FOREIGN_QEV_PREFIXES', 'new_root_state',
           'init_root', 'execute_root_query', 'execute_root_evidence',
           'validate_root_refs', 'root_status',
           'CTX_FETCH_CONTRACT_VERSION', 'ctx_fetch', 'check_expected_versions',
           'SPECIALIST_CONTRACT_VERSION', 'SPECIALIST_MAX_ITERATIONS',
           'SPECIALIST_REQUEST_KEYS', 'COMPLETION_STATES',
           'validate_specialist_request', 'resolve_evidence_refs',
           'assemble_specialist_task_input', 'stub_specialist_runner',
           'parse_specialist_output', 'specialist_init', 'execute_root_specialist',
           'specialist_report', 'specialist_status']
