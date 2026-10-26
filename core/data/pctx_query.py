# -*- coding: utf-8 -*-
"""按运行隔离的周期查询与证据登记服务。

查询语义委托 qctx_period；本模块维护 query_id、调用计数与 pqev 证据命名空间。
单期取证返回 pev 引用，并检查对象、属性、状态与窗口一致性。
冲突或身份不一致时阻断取证；单期证据不携带另一周期的状态变化。
只读、无模型或文件 IO，缺失与不可比不替换为零。"""
import json

from . import qctx_period as P               # noqa: E402
from . import period_view_generator as PVG   # noqa: E402

# 入口合同版本与命名空间前缀（与 qctx_v1 入口、ev: 工具证据三者互斥）
PERIOD_ENTRY_CONTRACT_VERSION = 'pctx_entry_v1'
PERIOD_QEV_PREFIX = 'pqev'
PERIOD_EVIDENCE_PREFIX = 'pev'
FOREIGN_QEV_PREFIXES = ('qev:', 'ev:')     # 跨命名空间引用=显式拒绝，不做别名互转

AGGREGATE_TOTAL_PREFIX = 'category_total'
# value `group_by='category'` 的**本次子集**合计键（完整品类合计另用
# `category_total:<metric>:<category>`；两种键在同一响应里不得互相代替）。
AGGREGATE_SELECTED_TOTAL_PREFIX = 'category_selected_total'
# 目录 category 缺失/为空时的稳定占位键（与 qctx_period._category_key 的
# _AGG_UNCATEGORIZED 同值；键形只做身份标识，不回填品类事实）。
AGGREGATE_UNCATEGORIZED_KEY = '(uncategorized)'
MAX_PERIOD_QUERIES_PER_RUN = 6

REJECTED_EVENT = 'period_rejected'
BUDGET_EVENT = 'budget_reached'


def new_run_state():
    """创建按 run_id 隔离的查询计数与引用登记表。

    query_id 按每次尝试递增，包含被拒请求；额度计数只统计实际取数。
    两者分开，避免拒绝和成功响应复用相同 ID。"""
    return {'count': 0, 'attempt': 0, 'executed': [], 'executed_by_norm': {}, 'qev_set': set()}


def _qev_ids(query_id, keys):
    return ['%s:%s:%s' % (PERIOD_QEV_PREFIX, query_id, k) for k in keys]


def _req_fingerprint(req):
    return json.dumps(req, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def category_total_ref_key(category) -> str:
    """品类汇总引用键的品类身份段：目录 category 原值；缺失/为空走稳定占位键。

    与 `oagent_ctx._locate_ref_content` 的品类匹配共用同一归一（None/'' 均视作占位），
    保证「签发用什么键、回查就匹配什么键」，不引入第二套判定。
    """
    cat = category if isinstance(category, str) else ''
    return cat if cat else AGGREGATE_UNCATEGORIZED_KEY


def _category_total_key(metric, entry):
    """value 品类合计引用键（完整品类 vs 本次子集分键；与 Data 侧 ref_key 同形）。"""
    cat = category_total_ref_key(entry.get('category'))
    if entry.get('member_scope') == 'selected_members_only':
        return 'category_selected_total:%s:%s' % (metric, cat)
    return '%s:%s:%s' % (AGGREGATE_TOTAL_PREFIX, metric, cat)


def _value_category_total_keys(out):
    """value `group_by='category'` 响应 → 实际返回的品类合计引用键集合。

    - `member_scope=full_category` 的组 `sum` → `category_total:<metric>:<category>`；
    - `member_scope=selected_members_only` 的组 `sum` → `category_selected_total:...`；
    - 子集组的 `full_category_total`（贡献度分母）→ 另签 `category_total:<metric>:<category>`。
    同一响应里两种键不互相代替，也不为未返回的品类签引用。
    """
    keys = []
    totals = out.get('category_totals')
    if not isinstance(totals, dict):
        return keys
    for metric, block in totals.items():
        if not isinstance(block, dict):
            continue
        for entry in (block.get('categories') or []):
            if not isinstance(entry, dict):
                continue
            keys.append(_category_total_key(metric, entry))
            full = entry.get('full_category_total')
            if isinstance(full, dict):
                keys.append('%s:%s:%s' % (AGGREGATE_TOTAL_PREFIX, metric,
                                          category_total_ref_key(entry.get('category'))))
    return keys


def _annotate_scope_refs(out, query_id):
    """把 Data 侧引用**键**补成 canonical `pqev:<query_id>:<key>`（与 qev 同源同形）。

    Data 层只算数值与键形（不知道 query_id）；服务面在签 qev 的同一处补全响应内
    `category_totals.*.categories[].ref`、`full_category_total.ref` 与行内
    `contribution.denominator_ref`，使模型/页面可直接引用而无需自行拼引用。
    """
    totals = out.get('category_totals')
    if isinstance(totals, dict):
        for block in totals.values():
            if not isinstance(block, dict):
                continue
            for entry in (block.get('categories') or []):
                if not isinstance(entry, dict):
                    continue
                ref_key = entry.pop('ref_key', None)
                if ref_key:
                    entry['ref'] = '%s:%s:%s' % (PERIOD_QEV_PREFIX, query_id, ref_key)
                full = entry.get('full_category_total')
                if isinstance(full, dict):
                    full_key = full.pop('ref_key', None)
                    if full_key:
                        full['ref'] = '%s:%s:%s' % (PERIOD_QEV_PREFIX, query_id, full_key)
    for row in (out.get('results') or []):
        contribution = row.get('contribution') if isinstance(row, dict) else None
        if isinstance(contribution, dict):
            ref_key = contribution.pop('denominator_ref_key', None)
            if ref_key:
                contribution['denominator_ref'] = '%s:%s:%s' % (
                    PERIOD_QEV_PREFIX, query_id, ref_key)


def _diff_metric_blocks(out):
    """diff 响应 → 逐指标块列表（单指标与多指标两种形状统一取法）。

    - 多指标：`by_metric.<metric>`，每块自带 `results` 与 `summary`；
    - 单指标（现状）：响应顶层即单指标块（`results` + `summary`），指标取顶层 `metric`。

    返回 [(metric, results, summary)]；形状不符返回 []。
    """
    by_metric = out.get('by_metric')
    if isinstance(by_metric, dict) and by_metric:
        blocks = []
        for metric, block in by_metric.items():
            if not isinstance(block, dict):
                continue
            blocks.append((str(metric), block.get('results') or [],
                           block.get('summary') if isinstance(block.get('summary'), dict) else {}))
        return blocks
    metric = out.get('metric')
    if not metric:
        return []
    return [(str(metric), out.get('results') or [],
             out.get('summary') if isinstance(out.get('summary'), dict) else {})]


def _diff_category_total_key(metric, entry, multi):
    """生成 diff 品类引用键：多指标包含 metric 段，单指标只包含品类段。

    周期与日期窗口比较共用该规则，避免同品类不同指标撞键。"""
    cat = category_total_ref_key(entry.get('category'))
    if multi:
        return '%s:%s:%s' % (AGGREGATE_TOTAL_PREFIX, metric, cat)
    return '%s:%s' % (AGGREGATE_TOTAL_PREFIX, cat)


def _result_qev_keys(query, out):
    """按 pctx 响应形状登记**实际返回对象**的引用键（引用不得支持未返回对象）。"""
    op = str(query.get('op') or '')
    event = str(out.get('event') or '')
    keys = []
    if event == 'period_window_values':
        win = out.get('window') or {}
        keys.append('window:%s~%s' % (win.get('start_date'), win.get('end_date')))
        for blk in (out.get('results') or []):
            keys.append('window_object:%s:%s' % (blk.get('product_id'), blk.get('metric')))
        keys.extend(_value_category_total_keys(out))
    elif event == 'period_window_diff_set':
        # 日期窗口多指标与周期多指标同形——品类合计引用同样带指标段（/同品类不撞键）。
        multi = isinstance(out.get('by_metric'), dict) and bool(out.get('by_metric'))
        for metric, results, summary in _diff_metric_blocks(out):
            for r in results:
                keys.append('window_diff:%s:%s:%s' % (r.get('product_id'), r.get('metric'),
                                                      r.get('state')))
            for entry in ((summary.get('category_totals') or {}).get('categories') or []):
                if isinstance(entry, dict):
                    keys.append(_diff_category_total_key(metric, entry, multi))
    elif event == 'period_window_set_compare':
        win = out.get('window') or {}
        keys.append('window_compare:%s:%s~%s:%s' % (out.get('metric'), win.get('start_date'),
                                                    win.get('end_date'), out.get('kind')))
        for item in (out.get('ranking') or []):
            keys.append('window_compare_row:%s' % item.get('product_id'))
        for item in ((out.get('high_group') or []) + (out.get('equal_group') or [])
                     + (out.get('low_group') or [])):
            keys.append('window_compare_row:%s' % item)
        for item in (out.get('missing_group') or []):
            keys.append('window_compare_missing:%s' % item.get('product_id'))
    elif event == 'period_daily_series':
        win = out.get('window') or {}
        keys.append('window:%s~%s' % (win.get('start_date'), win.get('end_date')))
        for r in (out.get('results') or []):
            keys.append('day_series:%s:%s' % (r.get('product_id'), r.get('metric')))
    elif op == 'value':
        pid = str(out.get('period', {}).get('period_id') or query.get('period_id') or '')
        keys.append('period:%s' % pid)
        for blk in (out.get('results') or []):
            if blk.get('state') == P.ST_OK:
                keys.append('object:%s:%s:%s' % (blk.get('product_id'), blk.get('period_id'),
                                                 blk.get('metric')))
        for st in (out.get('object_period_states') or []):
            keys.append('state:%s:%s' % (st.get('product_id'), st.get('period_id')))
        keys.extend(_value_category_total_keys(out))
    elif op == 'diff':
        # 多指标品类引用包含 metric 段，单指标使用 category_total:<category>；键形由统一函数生成。
        multi = isinstance(out.get('by_metric'), dict) and bool(out.get('by_metric'))
        for metric, results, summary in _diff_metric_blocks(out):
            for r in results:
                keys.append('diff:%s:%s:%s' % (r.get('product_id'), r.get('metric'),
                                               r.get('state')))
            for entry in ((summary.get('category_totals') or {}).get('categories') or []):
                if isinstance(entry, dict):
                    keys.append(_diff_category_total_key(metric, entry, multi))
    elif op == 'set_compare':
        keys.append('compare:%s:%s:%s' % (out.get('metric'), (out.get('period') or {}).get('period_id'),
                                          out.get('kind')))
        for item in (out.get('ranking') or []):
            keys.append('compare_row:%s' % item.get('product_id'))
        for item in (out.get('high_group') or []) + (out.get('equal_group') or []) + (out.get('low_group') or []):
            keys.append('compare_row:%s' % item)
        for item in (out.get('missing_group') or []):
            keys.append('compare_missing:%s' % item.get('product_id'))
    elif op == 'states':
        for o in (out.get('objects') or []):
            keys.append('object_state:%s' % o.get('product_id'))
    elif op == 'catalog':
        keys.append('catalog:%s' % out.get('view_version'))
    elif op == 'cohort_filter':
        scope_block = out.get('scope') or {}
        scope_kind = str(scope_block.get('kind') or '')
        # `category` 形态的 scope 身份带**具体品类**（目录序、`+` 连接）——
        # `cohort:category:<cat1>+<cat2>:<period…>`。`benchmark_all` /
        if scope_kind == 'category':
            scope_identity = 'category:%s' % '+'.join(
                str(c) for c in (scope_block.get('categories') or []))
        else:
            scope_identity = scope_kind
        if out.get('time_mode') == 'single_period':
            keys.append('cohort:%s:%s' % (scope_identity, out.get('period_id')))
        else:
            keys.append('cohort:%s:%s:%s' % (scope_identity, out.get('base_period_id'),
                                             out.get('target_period_id')))
    # `aggregate='sum'` 响应的单期集合级合计——每个可加总指标一条独立
    # `set_sum:<metric>` 集合引用；引用键区分 /、不挂在逐商品行引用上。
    # 共用签发路径覆盖**两种时间模式**：`period_values`（周期值）与
    # `period_window_values`（日期窗口值），`single_period_totals` 键面形状相同。
    spt = out.get('single_period_totals')
    if isinstance(spt, dict):
        for m_key, entry in spt.items():
            if isinstance(entry, dict) and entry.get('event') == 'single_period_totals':
                keys.append('set_sum:%s' % m_key)
    return [k for k in keys if not k.endswith(('None', ':'))]


def execute_query(payload, run_state=None, allowed_product_ids=None):
    """执行周期查询并登记响应与引用，错误请求返回结构化 period_rejected。

    根任务商品上界传给数据层，用于解析 benchmark_all；显式越界请求在根任务守卫拒绝。"""
    st = run_state if isinstance(run_state, dict) else new_run_state()
    if not isinstance(payload, dict):
        return {'event': REJECTED_EVENT, 'error_code': 'invalid_structure',
                'detail': 'query 必须是 JSON 对象', 'query_id': '', 'qev': []}
    req = payload.get('request')
    if not isinstance(req, dict):
        return {'event': REJECTED_EVENT, 'error_code': 'invalid_structure',
                'detail': 'query.request 必须是 JSON 对象', 'query_id': '', 'qev': []}

    # 外层合同必须先于 cache/budget 分支检查；仅按 request 命中缓存会让
    # contract_version 或未知顶层键的变化绕过 qctx_period 的合同拒绝。
    envelope_rejection = P.validate_period_query_envelope(payload)
    if envelope_rejection is not None:
        out = envelope_rejection
    else:
        fp = _req_fingerprint(req)
        cached = (st.get('executed_by_norm') or {}).get(fp)
        if cached:
            # 同 run 同请求=复用既有 query_id 与引用（不重复计数、不重复执行）
            return dict(cached, cache_hit=True)

        if int(st.get('count') or 0) >= MAX_PERIOD_QUERIES_PER_RUN:
            return {'event': BUDGET_EVENT, 'contract_version': P.PERIOD_CONTRACT_VERSION,
                    'detail': '本 run 周期查询已达上限 %d（预算语义沿 qctx §5.7）' % MAX_PERIOD_QUERIES_PER_RUN,
                    'count': int(st.get('count') or 0), 'qev': []}

        out = P.execute_period_query(payload, allowed_product_ids=allowed_product_ids)
    qid = 'p%d' % (int(st.get('attempt') or 0) + 1)
    st['attempt'] = int(st.get('attempt') or 0) + 1
    out = dict(out)
    out['query_id'] = qid
    out['entry_contract_version'] = PERIOD_ENTRY_CONTRACT_VERSION
    if out.get('event') == REJECTED_EVENT:
        out['qev'] = []
        st['executed'].append({'query_id': qid, 'event': REJECTED_EVENT,
                               'error_code': out.get('error_code'), 'request': req})
        # 结构拒绝不消耗查询预算（未执行取数，沿 qctx 侧语义）；query_id 仍单调唯一，
        # 失败响应不会被后续成功查询复用（防止按 query_id 索引时失败遮住成功）
        return out

    # 响应内品类合计/贡献分母引用补全为 canonical pqev（与下面签发的 qev 同源同形）。
    _annotate_scope_refs(out, qid)

    st['count'] = int(st.get('count') or 0) + 1
    qev = _qev_ids(qid, _result_qev_keys(req, out))
    out['qev'] = qev
    out['provenance'] = {
        'view_version': PVG.VIEW_VERSION, 'generator_version': PVG.GENERATOR_VERSION,
        'seed': PVG.DEFAULT_SEED,
        'demo_caliber': PVG.DEMO_CALIBER,
        'note': '周期事实只来自 pctx_v1 新视图；snapshot_id/case_id 属旧命名空间，不得替代 period_id',
    }
    st.setdefault('qev_set', set()).update(qev)
    st['executed'].append({'query_id': qid, 'event': out.get('event'), 'request': req, 'qev': qev})
    st['executed_by_norm'][fp] = out
    return out


def validate_qev_refs(run_state, refs):
    """周期证据引用动态守卫：每个引用必须 ∈ 本 run 实际执行的 `pqev:` 登记表。
    跨命名空间（`qev:` / `ev:`）=显式拒绝并单独登记，不做别名互转（ 数据源不混用）。"""
    registered = set((run_state or {}).get('qev_set') or set())
    refs = [str(r) for r in (refs or [])]
    accepted, rejected, foreign = [], [], []
    for r in refs:
        if r.startswith(FOREIGN_QEV_PREFIXES):
            rejected.append(r)
            foreign.append(r)
            continue
        if r in registered:
            accepted.append(r)
        else:
            rejected.append(r)
    return {
        'event': 'period_qev_guard', 'contract_version': PERIOD_ENTRY_CONTRACT_VERSION,
        'ok': not rejected, 'accepted': accepted, 'rejected': rejected,
        'foreign_namespace': foreign,
        'note': 'period 引用只支持本 run 实际返回对象；`qev:`（旧快照查询）与 `ev:`（Worker 七工具）'
                '属不同命名空间，不能为周期事实背书',
    }


# ===================== 同源单期取证适配（ 最小适配） =====================

def single_period_evidence(product_id, period_id, metrics=('M1', 'M2'), view=None):
    """从同一经营视图获取单期证据，校验对象身份、属性、状态与窗口。

    证据使用独立 pev 命名空间；身份或记录冲突时阻断取证，不返回数值。"""
    view = view if view is not None else P.build_view()
    pid = str(product_id or '')
    known = {str(o['product_id']) for o in view['objects']}
    if pid not in known:
        return {'ok': False, 'error_code': 'unknown_object',
                'detail': '对象 %r 不在新视图目录（周期取证不做旧快照回退）' % pid}
    periods = {p['period_id']: p for p in PVG.PERIODS}
    meta = periods.get(str(period_id))
    if meta is None:
        return {'ok': False, 'error_code': 'namespace_conflict',
                'detail': 'period_id=%r 不在新视图周期目录（旧 snapshot/case 身份不可替代周期）'
                          % str(period_id)}
    try:
        states = P.object_states(pid, view=view)
    except Exception as e:                                    # 身份/结构异常不掩盖
        return {'ok': False, 'error_code': 'identity_check_failed', 'detail': str(e)[:200]}
    # 状态判定按 object_states 真实返回形状（states 为列表，身份详情在 identity 子对象）：
    # - identity_inconsistent（稳定身份跨期不一致）与 record_conflict（同周期矛盾记录）=硬错误，
    #   阻止本证据被当作可靠取证（ok=False，不给 evidence_id，不返回数值）；
    # - state_change（published/price 跨期不同）=正常变化，只披露不阻断；
    # - attribute_drift（category/city 跨期不同）=待复核，披露不阻断（/B）。
    hard = [s for s in (states.get('states') or [])
            if s in (P.OBJ_IDENTITY_INCONSISTENT, P.OBJ_RECORD_CONFLICT)]
    values = P.period_values([pid], period_id, list(metrics), view=view)
    # 商品名/类目/城市从**当前周期 record**取（无记录时回退视图对象目录）——object_states
    # 顶层没有这些字段，之前从这里取值恒为 None。
    obj_catalog = next((o for o in view['objects'] if str(o['product_id']) == pid), {})
    recs = [r for r in view['records'] if str(r['product_id']) == pid and r['period_id'] == period_id]
    rec = recs[0] if recs else {}
    obj_meta = {'product_id': pid,
                'product_name': rec.get('product_name') or obj_catalog.get('product_name'),
                'category': rec.get('category') or obj_catalog.get('category'),
                'city': rec.get('city') or obj_catalog.get('city'),
                'meta_source': ('current_period_record' if recs else 'view_object_catalog')}
    if hard:
        return {'ok': False, 'error_code': hard[0],
                'detail': '对象 %s 在周期 %s 存在阻断状态 %s：本证据不可作为可靠取证'
                          '（%s）' % (pid, period_id, '/'.join(hard),
                                      '身份不一致' if hard[0] == P.OBJ_IDENTITY_INCONSISTENT
                                      else '同周期记录互相矛盾'),
                'object': obj_meta, 'period': dict(meta),
                'state_disclosure': _state_disclosure(states),
                'source_check': {
                    'view_version': view['view_version'], 'generator_version': view['generator_version'],
                    'old_snapshot_used': False, 'old_worker_used': False,
                    'namespace': 'period（snapshot_id/case_id 属旧命名空间，本证据不含其身份）',
                }}
    evidence_id = '%s:%s:%s' % (PERIOD_EVIDENCE_PREFIX, pid, period_id)
    return {
        'ok': True,
        'evidence_id': evidence_id,
        'evidence_layer': 'period_view（新视图单期取证，非旧 snapshot/Worker 工具）',
        'object': obj_meta,
        'period': dict(meta),
        'window_note': '周期 %s（%s ~ %s，%d 天）；与另一模拟周期等长且同口径方可差'
                       % (meta['period_label'], meta['period_start_date'], meta['period_end_date'],
                          meta['length_days']),
        'state_disclosure': _state_disclosure(states),
        'metric_values': values.get('results') or [],
        'metric_value_qev': None,                            # 数值引用走 /pctx_query 的 pqev
        'source_check': {
            'view_version': view['view_version'], 'generator_version': view['generator_version'],
            'old_snapshot_used': False, 'old_worker_used': False,
            'namespace': 'period（snapshot_id/case_id 属旧命名空间，本证据不含其身份）',
        },
        'demo_caliber': PVG.DEMO_CALIBER,
        'reviewer_note': '旧 Worker 七工具按单 snapshot 设计，不能承载 period_id 身份；'
                         '本适配不把周期数据交给旧 Worker，若需 Worker 级调查须先由人工 reviewer '
                         '确认最小合法方案，不允许隐式混用。',
    }


def _state_disclosure(states):
    """仅披露本周期记录冲突，不携带另一周期的状态或属性变化。

    跨期状态只用于内部硬错误检查；展示跨期变化须使用 states/diff。"""
    return {
        'record_conflicts': list(states.get('record_conflicts') or []),
        'cross_period_changes_out_of_scope': ['state_change', 'attribute_drift'],
        'note': '单期取证只披露当前周期属性/指标与本周期记录冲突；跨期状态/属性变化'
                '（state_change/attribute_drift）不随单期证据披露——查询它们属跨期操作，'
                '须走 states/diff 且本轮选择两期对比',
    }


__all__ = ['PERIOD_ENTRY_CONTRACT_VERSION', 'PERIOD_QEV_PREFIX', 'PERIOD_EVIDENCE_PREFIX',
           'FOREIGN_QEV_PREFIXES', 'MAX_PERIOD_QUERIES_PER_RUN', 'new_run_state',
           'execute_query', 'validate_qev_refs', 'single_period_evidence']
