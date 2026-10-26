# -*- coding: utf-8 -*-
"""模拟经营视图的只读取数、跨期比较、集合统计与条件筛选。

周期请求按商品 × 周期聚合；日期窗口使用日级事实，保持相同指标口径。
M1 为商品订单合计，M2 为播放合计，M3 为非缺失点击率条均，非曝光加权。
M4 为 eligible 配对行的订单/播放 ×1000；M5 为 eligible 配对行的播放加权成交额效率。
缺失、冲突、零分母和不可比分别返回明确状态，不静默代零或挑选冲突记录。
查询只使用固定模拟视图，不接受调用方指定随机种子。"""
import json
import math
import operator as _operator

from . import period_view_generator as PVG      # noqa: E402
from . import period_resolver as PR              # noqa: E402
from . import daily_aggregate as DAILY           # noqa: E402  （日级事实聚合只读层）
from .oi03_brief import CATEGORY_LABEL_PREFIX     # noqa: E402  （品类入口标签前缀单点）
from .query_contract import QueryRejected, require as _require  # noqa: E402
from .video_eligibility import video_eligibility  # noqa: E402

# 扩展版本与命名空间：与 qctx_v1 并列，互斥转义
PERIOD_CONTRACT_VERSION = 'pctx_v1'
PERIOD_ID_PREFIX = 'period_demo_'
OLD_ID_PREFIXES = ('snapshot_eval_', 'snapshot_rt-')   # 旧命名空间前缀（禁混用/禁别名互转）

# ---- M1–M5 指标登记：字段、公式与缺失口径 ----
METRICS = {
    'M1': {'key': 'M1', 'name': '订单量', 'unit': '单', 'role': '结果',
           'business_layer': 'result', 'aggregation_population': 'product_period_orders',
           'inference_limits': ['M1_M2_M4_order_decomposition_not_supported'],
           'source_layer': 'product（周期商品层）', 'source_field_or_formula': 'period_orders（recent_7d_orders 语义）',
           'formula': '周期成交单合计（7 天口径直接值）',
           'missing_rule': '字段缺失→field_missing；该周期无记录→period_missing'},
    'M2': {'key': 'M2', 'name': '播放量', 'unit': '次', 'role': '过程',
           'business_layer': 'distribution', 'aggregation_population': 'all_video_rows_with_vv',
           'inference_limits': ['M1_M2_M4_order_decomposition_not_supported'],
           'source_layer': 'row_hist', 'source_field_or_formula': 'vv',
           'formula': 'sum(vv) over 该(商品,周期)行',
           'missing_rule': '行缺失→excluded_missing，不进分母；无行→unknown(no_rows)'},
    'M3': {'key': 'M3', 'name': '点击率', 'unit': '比率（条均）', 'role': '过程',
           'business_layer': None, 'aggregation_population': 'video_rows_with_nonmissing_plc_ctr',
           'inference_limits': [],
           'source_layer': 'row_hist', 'source_field_or_formula': 'plc_ctr',
           'formula': '非缺失行的条均（分母=非缺失行数；非曝光加权）',
           'missing_rule': '缺行 excluded_missing；分母=0→zero_denominator'},
    'M4': {'key': 'M4', 'name': '千次播放订单（OPM）', 'unit': '单/千次播放', 'role': '结果',
           'business_layer': 'result', 'aggregation_population': 'eligible_video_rows_paired_vv_paid_orders',
           'inference_limits': ['M1_M2_M4_order_decomposition_not_supported'],
           'source_layer': 'row_hist（既有公式 funnel_opm）', 'source_field_or_formula': 'vv+paid_orders',
           'formula': 'Σpaid_orders/Σvv×1000（eligible 且同视频配对；缺失/不合格不按 0）',
           'missing_rule': 'Σvv=0 或无可配对行→unknown(zero_denominator)'},
    'M5': {'key': 'M5', 'name': '千次播放成交额（GPM）', 'unit': '元/千次播放', 'role': '结果',
           'business_layer': 'result', 'aggregation_population': 'eligible_video_rows_paired_vv_gpm',
           'inference_limits': [],
           'source_layer': 'row_hist（既有公式 gpm_vv_weighted）', 'source_field_or_formula': 'gpm+vv',
           'formula': 'Σ(gpm×vv)/Σvv（VV 加权；非曝光）',
           'missing_rule': 'Σvv=0→unknown(zero_denominator)，不回退（period_zero_denominator_no_fallback）'},
}
METRIC_KEYS = tuple(METRICS.keys())
COHORT_OP = 'cohort_filter'
# 日期窗口操作面。`daily_series` 是唯一新增 op（一次返回连续日读数＋范围汇总，
# 满足「最近几天连续读数不必逐日查」）；其余日期能力复用在既有 value/diff/set_compare 上。
DAILY_SERIES_OP = 'daily_series'
QUERY_OPS = ('value', 'diff', 'set_compare', 'states', 'catalog', COHORT_OP, DAILY_SERIES_OP)
# 请求键并集供 schema 引用；执行期按 OP_REQUEST_KEYS 校验各操作的闭集。
QUERY_REQUEST_KEYS = ('op', 'product_ids', 'period_id', 'metrics', 'metric', 'kind',
                      'aggregate', 'base_period_id', 'target_period_id',
                      'window', 'base_window', 'target_window')

# 指标差异状态与对象、记录状态分别定义，避免混用。
ST_OK = 'ok'
ST_ZERO_DENOM = 'zero_denominator'
ST_PERIOD_MISSING = 'period_missing'
ST_FIELD_MISSING = 'field_missing'
ST_NOT_APPLICABLE = 'not_applicable'
ST_WINDOW_INCOMPARABLE = 'window_incomparable'
DIFF_STATES = (ST_OK, ST_ZERO_DENOM, ST_PERIOD_MISSING, ST_FIELD_MISSING,
               ST_NOT_APPLICABLE, ST_WINDOW_INCOMPARABLE)

OBJ_IDENTITY_INCONSISTENT = 'identity_inconsistent'
OBJ_STATE_CHANGE = 'state_change'
OBJ_ATTRIBUTE_DRIFT = 'attribute_drift'
OBJ_RECORD_CONFLICT = 'record_conflict'

_METRIC_FIELD = {'M1': 'period_orders', 'M2': 'vv', 'M3': 'plc_ctr',
                 'M4': ('vv', 'paid_orders'), 'M5': ('gpm', 'vv')}
_DEFAULT_SET_METRIC = 'M4'
# set_compare 仅在 metric 键缺失时使用默认指标；metrics 数组不是该操作的合法参数。

_view_cache = {}


def _round_num(x):
    return None if x is None else round(float(x), 4)


def build_view(seed=PVG.DEFAULT_SEED):
    """取内部一致两期视图（按 seed memoize；纯内存，无文件 IO）。"""
    if seed not in _view_cache:
        _view_cache[seed] = PVG.build_operating_view(seed)
    return _view_cache[seed]


# ===================== E1：周期选择与命名空间互斥 =====================

def _validate_period_id(period_id):
    pid = str(period_id or '')
    _require(bool(pid), 'missing_period_id', 'period_id 必填（pctx_v1 作用域）')
    _require(not pid.startswith(OLD_ID_PREFIXES), 'namespace_conflict',
             'period_id=%r 属旧 snapshot/case 命名空间：新 period_id 与旧 snapshot_id/case_id '
             '分属不同命名空间，禁别名互转' % pid)
    _require(pid in PVG.PERIOD_IDS, 'unknown_period_id',
             'period_id=%r 不在合同周期集 %s' % (pid, '/'.join(PVG.PERIOD_IDS)))
    return pid


def _validate_metric(metric):
    m = str(metric or '')
    _require(m in METRICS, 'unknown_metric', '指标 %r 不在 pctx_v1 注册表 %s'
             % (m, '/'.join(METRIC_KEYS)))
    return m


def _view_records(view, product_ids=None, period_id=None):
    """按（对象集合 × period_id）取记录；保持视图自然序（商品序×周期序）。只读。"""
    ids = None if product_ids is None else {str(p) for p in product_ids}
    out = []
    for r in view['records']:
        if period_id is not None and r['period_id'] != period_id:
            continue
        if ids is not None and str(r['product_id']) not in ids:
            continue
        out.append(r)
    return out


SCOPE_KINDS = ('benchmark_all', 'category', 'product_ids')
SCOPE_KEYS = ('kind', 'categories', 'product_ids')
CATEGORY_MEMBER_SCOPE_FULL = 'full_category'
CATEGORY_MEMBER_SCOPE_SELECTED = 'selected_members_only'
GROUP_BY_VALUES = ('category',)
# 上限沿用 cohort 的 `COHORT_MAX_SCOPE_IDS`（下方定义；运行期解析，不在本处抄第二份数值）。


def _bound_pool(view, allowed_product_ids=None):
    """本轮可调查范围全池（目录序）：有根任务商品上界时＝上界内成员，缺省＝目录全部商品。

    `benchmark_all`（`resolve_scope`）与 `states` 缺省全池共用同一语义；上界只由入口显式选
    品类时登记。上界内成员恒按目录稳定序返回（不跟随上界传入顺序）。
    """
    catalog_ids = [str(o['product_id']) for o in view['objects']]
    bound_raw = {str(p) for p in (allowed_product_ids or []) if str(p or '')}
    if not bound_raw:
        return catalog_ids
    return [pid for pid in catalog_ids if pid in bound_raw]


def _category_label_name(raw):
    """品类名归一：接受目录原值或入口标签「品类 · <name>」，剥前缀；不做模糊匹配/别名。"""
    text = str(raw or '').strip()
    if text.startswith(CATEGORY_LABEL_PREFIX):
        text = text[len(CATEGORY_LABEL_PREFIX):].strip()
    return text


def _catalog_category_order(view):
    """目录品类首次出现顺序（不排序、不写死品类名；与 oi03_brief.category_options 同序）。"""
    order = []
    for obj in view['objects']:
        cat = obj.get('category')
        if isinstance(cat, str) and cat and cat not in order:
            order.append(cat)
    return order


def _scope_categories_block(view, member_set):
    """本次实际成员按目录品类分组（只列被触及的品类；目录完整成员数 vs 实际成员数）。"""
    members_by_cat = {}
    for obj in view['objects']:
        cat = obj.get('category')
        cat = cat if isinstance(cat, str) and cat else _AGG_UNCATEGORIZED
        members_by_cat.setdefault(cat, []).append(str(obj['product_id']))
    block = []
    for cat in _catalog_category_order(view):
        full = members_by_cat.get(cat) or []
        selected = [pid for pid in full if pid in member_set]
        if not selected:
            continue
        block.append({'category': cat,
                      'catalog_member_count': len(full),
                      'member_count': len(selected),
                      'members': selected,
                      'coverage_complete': len(selected) == len(full)})
    return block


def resolve_scope(scope, view=None, allowed_product_ids=None):
    """解析共享业务范围 → `{normalized, members, resolution}`。

    - `normalized`＝规范回显（`benchmark_all` 无额外键；`product_ids` 按目录稳定序）；
    - `members`＝解析后的商品 ID（恒目录稳定序，不跟随请求顺序）；
    - `resolution`＝`scope_resolution` 响应块。
    `allowed_product_ids`（根任务商品上界）只用于把 `benchmark_all` 解析为「上界内全池」，
    并据此披露 `bound`；显式 `category` / `product_ids` 越界由根任务 D 类守卫先行拒绝
    （本函数不静默裁剪请求范围）。
    """
    view = view if view is not None else build_view()
    _require(isinstance(scope, dict), 'missing_scope', 'scope 必须是 JSON 对象')
    extra = sorted(set(scope.keys()) - set(SCOPE_KEYS))
    _require(not extra, 'unknown_keys', 'scope 未知键: %s（只接受 %s）'
             % (','.join(extra), '/'.join(SCOPE_KEYS)))
    kind = scope.get('kind')
    _require(kind in SCOPE_KINDS, 'unknown_scope_kind',
             'scope.kind=%r 不在 %s' % (kind, '/'.join(SCOPE_KINDS)))

    catalog_ids = [str(o['product_id']) for o in view['objects']]
    catalog_set = set(catalog_ids)
    bound_raw = [str(p) for p in (allowed_product_ids or []) if str(p or '')]
    bound_ids = [pid for pid in catalog_ids if pid in set(bound_raw)] if bound_raw else None
    bound_set = set(bound_ids) if bound_ids is not None else None

    requested = {'kind': kind}
    if kind == 'benchmark_all':
        _require('categories' not in scope and 'product_ids' not in scope, 'unknown_keys',
                 'scope.kind=benchmark_all 不接受 categories/product_ids')
        normalized = {'kind': 'benchmark_all'}
        members = _bound_pool(view, allowed_product_ids)
    elif kind == 'category':
        _require('product_ids' not in scope, 'unknown_keys',
                 'scope.kind=category 不接受 product_ids')
        raw = scope.get('categories')
        _require(isinstance(raw, list), 'invalid_structure', 'scope.categories 必须是数组')
        _require(bool(raw), 'empty_scope', 'scope.categories 不可为空（无法定义范围）')
        requested['categories'] = [str(c) for c in raw]
        names = [_category_label_name(c) for c in raw]
        seen, dups = set(), []
        for name in names:
            if name in seen and name not in dups:
                dups.append(name)
            seen.add(name)
        _require(not dups, 'duplicate_category',
                 'scope.categories 重复（不静默去重）: %s' % ','.join(dups))
        catalog_categories = _catalog_category_order(view)
        unknown = [name for name in names if name not in catalog_categories]
        _require(not unknown, 'unknown_category',
                 '品类 %s 不在本视图目录品类集 %s（不做模糊匹配/别名）'
                 % ('/'.join(unknown), '/'.join(catalog_categories)))
        wanted = {name for name in names if name}
        normalized = {'kind': 'category',
                      'categories': [c for c in catalog_categories if c in wanted]}
        members = [str(o['product_id']) for o in view['objects']
                   if (o.get('category') if isinstance(o.get('category'), str) else '') in wanted]
    else:
        _require('categories' not in scope, 'unknown_keys',
                 'scope.kind=product_ids 不接受 categories')
        raw = scope.get('product_ids')
        _require(isinstance(raw, list), 'invalid_structure', 'scope.product_ids 必须是数组')
        _require(bool(raw), 'empty_scope', 'scope.product_ids 不可为空（分母为 0 无法定义）')
        requested['product_ids'] = [str(p) for p in raw]
        pids = [str(p) for p in raw]
        seen, dups = set(), []
        for pid in pids:
            if pid in seen and pid not in dups:
                dups.append(pid)
            seen.add(pid)
        _require(not dups, 'duplicate_product_id',
                 'scope.product_ids 重复（不静默去重）: %s' % ','.join(dups))
        unknown_ids = [pid for pid in pids if pid not in catalog_set]
        _require(not unknown_ids, 'unknown_object',
                 'scope.product_ids 不在本视图目录: %s' % ','.join(unknown_ids))
        normalized = {'kind': 'product_ids',
                      'product_ids': [pid for pid in catalog_ids if pid in set(pids)]}
        members = list(normalized['product_ids'])

    if kind in ('category', 'product_ids'):
        _require(len(members) <= COHORT_MAX_SCOPE_IDS, 'scope_too_large',
                 'scope 解析后 %d 个成员超过当前上限 %d（不截断、不分页）'
                 % (len(members), COHORT_MAX_SCOPE_IDS))

    member_set = set(members)
    categories = _scope_categories_block(view, member_set)
    duplicates = _duplicate_members_across_categories(view, members)
    if bound_ids is None:
        bound = {'kind': 'catalog_default', 'allowed_count': len(catalog_ids),
                 'catalog_count': len(catalog_ids), 'complete': True}
    else:
        bound = {'kind': 'run_allowed_products', 'allowed_count': len(bound_ids),
                 'catalog_count': len(catalog_ids),
                 'complete': len(bound_ids) == len(catalog_ids)}
    if kind == 'benchmark_all':
        coverage_complete = True
        coverage_note = ('本次范围＝本轮可调查范围全池（%s）'
                         % ('根任务商品上界内全部商品' if bound_ids is not None else '目录全部商品'))
    elif kind == 'category':
        coverage_complete = all(c['coverage_complete'] for c in categories)
        coverage_note = ('本次范围＝目录完整品类成员，未截断' if coverage_complete
                         else '本次范围只覆盖所请求品类的部分成员（见 categories[].member_count）')
    else:
        coverage_complete = True
        coverage_note = '本次范围＝请求显式商品集合（已按目录序规范化）'
    resolution = {
        'requested': requested,
        'resolved_kind': kind,
        'categories': categories,
        'members': members,
        'member_count': len(members),
        'duplicate_members_across_categories': duplicates,
        'bound': bound,
        'coverage': {'complete': coverage_complete, 'note': coverage_note},
        'order': 'catalog',
        'catalog_version': view.get('view_version'),
    }
    return {'normalized': normalized, 'members': members, 'resolution': resolution}


def _resolve_query_scope(req, view, allowed_product_ids):
    """解析互斥的 scope 或顶层 product_ids 范围写法。

    scope 按目录解析成员并回显 scope_resolution；顶层 product_ids 保留请求成员形式。"""
    has_scope = 'scope' in req
    _require(not (has_scope and 'product_ids' in req), 'scope_conflict',
             '`scope` 与顶层 `product_ids` 同时出现：不猜哪个优先，只接受一种范围写法')
    if has_scope:
        resolved = resolve_scope(req.get('scope'), view=view,
                                 allowed_product_ids=allowed_product_ids)
        return resolved['members'], resolved['resolution']
    pids = req.get('product_ids')
    _require(isinstance(pids, list), 'invalid_structure', 'request.product_ids 必须是数组')
    return pids, None


def _duplicate_members_across_categories(view, members):
    """跨品类重复成员披露（目录理论上不重复；出现时保留一次并显式说明）。"""
    seen, dups = set(), []
    for pid in members:
        if pid in seen and pid not in dups:
            dups.append(pid)
        seen.add(pid)
    return dups


# ===================== 行身份消解（–共用；缺失≠0、冲突键整体退出） =====================

def _row_fingerprint(row):
    """一行 15 键内容的规范化指纹（变体消解用）。"""
    return json.dumps([row.get(k) for k in PVG.VIDEO_ROW_KEYS], ensure_ascii=False, sort_keys=True)


def _resolve_rows(record):
    """返回去冲突视频行、冲突数量与各字段缺失计数。

    同 video_id 的互异记录整体排除；None/空串不进入对应指标分母。"""
    by_key = {}
    for row in record.get('video_rows') or []:
        by_key.setdefault(str(row.get('video_id')), []).append(row)
    clean, conflicting = [], 0
    for key in sorted(by_key):
        variants = by_key[key]
        if len(variants) == 1:
            clean.append(variants[0])
            continue
        canon = {_row_fingerprint(r) for r in variants}
        if len(canon) > 1:
            conflicting += 1        # 同键 ≥2 种内容：冲突键整体退出，不择一/不平均
        else:
            clean.append(variants[0])
    missing = {f: sum(1 for r in clean if r.get(f) is None or r.get(f) == '')
               for f in ('vv', 'plc_ctr', 'paid_orders', 'gpm')}
    return clean, conflicting, missing


def _base(record, metric):
    m = METRICS[metric]
    period = PVG.period_by_id(record['period_id']) or {}
    return {
        'metric': metric, 'metric_name': m['name'], 'unit': m['unit'],
        **_metric_semantics(metric),
        'product_id': str(record['product_id']), 'product_name': record.get('product_name'),
        'period_id': record['period_id'], 'period_label': record.get('period_label'),
        'period_start_date': record.get('period_start_date'),
        'period_end_date': record.get('period_end_date'),
        'period_length_days': record.get('period_length_days'),
        'window_note': '周期 %s（%s ~ %s，%s 天）；与另一周期等长且同口径方可差'
                       % (record.get('period_label'), record.get('period_start_date'),
                          record.get('period_end_date'), record.get('period_length_days')),
        'demo_caliber': record.get('caliber_note') or PVG.DEMO_CALIBER,
        'period_meta_echo': dict(period),
    }


def _metric_semantics(metric):
    m = METRICS[metric]
    return {key: (list(m[key]) if key == 'inference_limits' else m[key]) for key in (
        'role', 'business_layer', 'aggregation_population', 'inference_limits',
        'source_layer', 'source_field_or_formula', 'formula', 'missing_rule')}


def _unknown(block, value_detail, reason):
    block.update(value='unknown(%s)' % value_detail, state=reason,
                 unknown_reason=reason, value_detail=value_detail)
    return block


def metric_value(record, metric):
    """单个（记录 × 指标）周期值：数值或明确 unknown/zero_denominator，**不静默代 0**。
    denominator 一律披露（求和类指标披露「无分母 + 参与行数」）。"""
    metric = _validate_metric(metric)
    rows, conflicting, missing = _resolve_rows(record)
    block = _base(record, metric)
    block.update(n_rows=len(rows), excluded_conflicting=conflicting,
                 excluded_missing=dict(missing))

    if metric == 'M1':
        val = record.get('period_orders')
        block['denominator'] = {'kind': 'sum_no_denominator', 'value': None,
                                'note': '合计指标（7 天口径直接值）；无分母'}
        if val is None:
            return _unknown(block, 'field_missing', ST_FIELD_MISSING)
        block.update(value=val, state=ST_OK)
        return block

    if metric == 'M2':
        block['denominator'] = {'kind': 'sum_no_denominator', 'value': None,
                                'note': 'sum(vv)：无分母；参与行数=非缺失 vv 行数'}
        if not rows:
            return _unknown(block, 'no_rows', ST_FIELD_MISSING)
        vals = [r.get('vv') for r in rows if r.get('vv') is not None]
        if not vals:
            return _unknown(block, 'field_all_missing', ST_FIELD_MISSING)
        block.update(value=_round_num(sum(vals)), contributing_rows=len(vals), state=ST_OK)
        return block

    if metric == 'M3':
        block['denominator'] = {'kind': 'row_count', 'value': len(rows) - missing['plc_ctr'],
                                'note': '条均口径：分母=非缺失 plc_ctr 行数（非曝光加权）'}
        vals = [r.get('plc_ctr') for r in rows if r.get('plc_ctr') is not None]
        # 有效分母=0（无行，或有行但 plc_ctr 全缺失）→ zero_denominator。
        if not vals:
            return _unknown(block, 'zero_denominator', ST_ZERO_DENOM)
        block.update(value=_round_num(sum(vals) / len(vals)), contributing_rows=len(vals), state=ST_OK)
        return block

    elig = [r for r in rows if video_eligibility(r)[0]]
    block['eligible_rows'] = len(elig)
    block['ineligible_rows'] = len(rows) - len(elig)

    if metric == 'M4':
        o_sum = v_sum = 0.0
        paired = 0
        for r in elig:
            o, v = r.get('paid_orders'), r.get('vv')
            if o is not None and v is not None:
                o_sum += o
                v_sum += v
                paired += 1
        block.update(paired_rows=paired,
                     denominator={'kind': 'sum_vv', 'value': _round_num(v_sum),
                                  'note': 'Σvv（eligible 且 vv、paid_orders 配对行）'})
        if paired == 0 or v_sum <= 0:
            return _unknown(block, 'zero_denominator', ST_ZERO_DENOM)
        block.update(value=_round_num(o_sum / v_sum * 1000.0),
                     numerator_orders=_round_num(o_sum), state=ST_OK)
        return block

    # M5 按 VV 加权；零分母不回退（period_zero_denominator_no_fallback）
    g_num = g_den = 0.0
    g_with = 0
    for r in elig:
        g, v = r.get('gpm'), r.get('vv')
        if g is None:
            continue
        g_with += 1
        if v is not None:
            g_num += g * v
            g_den += v
    block['eligible_gpm_rows'] = g_with
    block['gpm_agg'] = None
    block['denominator'] = {'kind': 'sum_vv', 'value': _round_num(g_den),
                            'note': 'Σvv（eligible 且 gpm、vv 配对行；VV 加权分母）'}
    if g_den > 0:
        block.update(value=_round_num(g_num / g_den), gpm_agg='vv_weighted', state=ST_OK)
        return block
    if g_with > 0:
        block['gpm_agg'] = 'period_zero_denominator_no_fallback'
        block['zero_denominator_note'] = PVG.GPM_ZERO_DENOMINATOR_NOTE
        return _unknown(block, 'zero_denominator', ST_ZERO_DENOM)
    return _unknown(block, 'field_all_missing', ST_FIELD_MISSING)


# ===================== E2：周期级取数 =====================

def period_values(product_ids, period_id, metrics, view=None, aggregate=None,
                  group_by=None, scope_resolution=None):
    """按对象、周期和指标读取数值，并披露单位、窗口、分母与状态。

    无记录返回 period_missing，矛盾记录返回 record_conflict。
    aggregate=sum 仅对 M1/M2 生成集合合计；group_by=category 同时生成品类合计和成员贡献。
    scope_resolution 仅在请求使用 scope 时返回，所有合计在数据层完成。"""
    view = view if view is not None else build_view()
    period_id = _validate_period_id(period_id)
    metrics = [_validate_metric(m) for m in metrics]
    _require(metrics, 'missing_metrics', 'metrics 须为非空数组')
    pids = [str(p) for p in (product_ids or [])]
    _require(pids, 'missing_objects', 'product_ids 须为非空数组')
    known = {str(o['product_id']) for o in view['objects']}
    unknown_ids = sorted(set(pids) - known)
    _require(not unknown_ids, 'unknown_object',
             '对象不在本视图目录: %s' % ','.join(unknown_ids))

    results, states = [], []
    for pid in sorted(set(pids)):
        recs = _view_records(view, [pid], period_id)
        if not recs:
            for m in metrics:
                block = {'metric': m, 'metric_name': METRICS[m]['name'], 'unit': METRICS[m]['unit'],
                         **_metric_semantics(m),
                         'product_id': pid, 'period_id': period_id,
                         'demo_caliber': PVG.DEMO_CALIBER}
                results.append(_unknown(block, 'period_missing', ST_PERIOD_MISSING))
            states.append({'product_id': pid, 'period_id': period_id, 'state': ST_PERIOD_MISSING})
            continue
        if len(recs) > 1 and len({_record_fingerprint(r) for r in recs}) > 1:
            for m in metrics:
                results.append(_unknown({**_base(recs[0], m), 'record_ids': [r['record_id'] for r in recs]},
                                        'record_conflict', OBJ_RECORD_CONFLICT))
            states.append({'product_id': pid, 'period_id': period_id,
                           'state': OBJ_RECORD_CONFLICT, 'record_ids': [r['record_id'] for r in recs]})
            continue
        rec = recs[0]
        for m in metrics:
            results.append(metric_value(rec, m))
        states.append({'product_id': pid, 'period_id': period_id, 'state': ST_OK})
    out = {
        'event': 'period_values',
        'contract_version': PERIOD_CONTRACT_VERSION,
        'period': dict(PVG.period_by_id(period_id)),
        'metrics_requested': metrics,
        'objects': sorted(set(pids)),
        'results': results,
        'object_period_states': states,
        'demo_caliber': PVG.DEMO_CALIBER,
    }
    if scope_resolution is not None:
        out['scope_resolution'] = scope_resolution
    if aggregate is not None:
        _require(aggregate in AGGREGATE_VALUES, 'aggregate_invalid',
                 'aggregate 仅支持 "sum"；当前=%r' % (aggregate,))
        spt = {}
        for m in metrics:
            if m not in AGGREGATE_METRICS:
                _require(False, 'aggregate_metric_not_supported',
                         '单期集合合计仅支持 M1（订单量）/ M2（播放量）；指标 %s 不支持 '
                         'aggregate=sum（M3/M4/M5 为比率或效率指标，不可跨商品直接求和）。'
                         '若需该指标的逐商品数值，改用不带 aggregate 的 value；'
                         '若需集合排名或中位数比较，改用 set_compare（kind=rank 或 kind=median）。'
                         % m)
            entry = single_period_totals(results, m, pids)
            if entry is not None:
                spt[m] = entry
        if spt:
            out['single_period_totals'] = spt
    _require_group_by(group_by, aggregate)
    if group_by is not None:
        totals, aggregates = _value_category_totals(
            metrics, pids, lambda pid, m: _resolve_metric_block(view, pid, period_id, m), view)
        out['category_totals'] = totals
        _attach_value_contributions(results, aggregates, view)
    return out


def _require_group_by(group_by, aggregate):
    """`group_by` 闭集与前置条件（Schema 先行；数据层兜底同一纪律）。"""
    if group_by is None:
        return
    _require(group_by in GROUP_BY_VALUES, 'group_by_invalid',
             'group_by 仅支持 %s；当前=%r' % ('/'.join(GROUP_BY_VALUES), group_by))
    _require(aggregate is not None, 'group_by_requires_aggregate',
             'group_by=%r 必须同时给 aggregate="sum"（分组合计是聚合结果，'
             '普通逐商品取值不产出品类合计）' % (group_by,))


def _record_fingerprint(rec):
    """记录内容指纹（用于 record_conflict 判定）：排除 record_id/view 版本等非事实噪声。"""
    payload = {k: rec.get(k) for k in ('product_id', 'product_name', 'category', 'city', 'price',
                                       'published', 'period_id', 'period_orders')}
    payload['video_rows'] = rec.get('video_rows')
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# ===================== E3：跨期差值（含全部差异状态码） =====================

def period_diff(product_id, metric, base_period=None, target_period=None, view=None):
    """计算同对象同指标的 target−base 及百分比变化。

    两期须同身份、同口径且窗口可比；基期为零时百分比不可用，其它缺失或冲突按原因返回。"""
    view = view if view is not None else build_view()
    metric = _validate_metric(metric)
    # 默认基期/末期=命名常量；与 PERIOD_IDS[0]/[1] 恒等（钉在 agents_oagent/tests/test_sync_nails.py），8 周期扩容后不显式传周期的差值仍=p1→p2，不静默漂移。
    base_period = _validate_period_id(base_period or PVG.DEFAULT_BASE_PERIOD_ID)
    target_period = _validate_period_id(target_period or PVG.DEFAULT_TARGET_PERIOD_ID)
    pid = str(product_id)
    known = {str(o['product_id']) for o in view['objects']}
    _require(pid in known, 'unknown_object', '对象 %r 不在本视图目录' % pid)
    # 身份门禁只覆盖实际比较的两期：两期身份一致时，窗口外的历史名称变化不阻断本次比较。
    # 两期都没有记录时 object_states 返回 period_missing，后续仍由原 period_missing 分支处理。
    identity = object_states(pid, view=view, base_period_id=base_period,
                             target_period_id=target_period)
    if OBJ_IDENTITY_INCONSISTENT in identity.get('states', []):
        return {
            'event': 'period_diff', 'contract_version': PERIOD_CONTRACT_VERSION,
            'metric': metric, 'metric_name': METRICS[metric]['name'], 'unit': METRICS[metric]['unit'],
            'product_id': pid, 'base_period_id': base_period, 'target_period_id': target_period,
            **_metric_semantics(metric), 'demo_caliber': PVG.DEMO_CALIBER,
            'state': OBJ_IDENTITY_INCONSISTENT,
            'unknown_reason': OBJ_IDENTITY_INCONSISTENT,
            'state_note': '稳定身份字段跨期不一致：停止该对象比较，不计算差值',
            'identity': identity['identity'], 'absolute_diff': 'unknown(identity_inconsistent)',
            'percent_diff': 'unknown(identity_inconsistent)',
        }

    out = {'event': 'period_diff', 'contract_version': PERIOD_CONTRACT_VERSION,
           'metric': metric, 'metric_name': METRICS[metric]['name'], 'unit': METRICS[metric]['unit'],
           'product_id': pid, 'base_period_id': base_period, 'target_period_id': target_period,
           **_metric_semantics(metric), 'demo_caliber': PVG.DEMO_CALIBER,
           'absolute_diff_formula': '值_target − 值_base（同单位）',
           'percent_diff_formula': '(值_target − 值_base) / 值_base × 100%（仅当值_base≠0）'}

    base_block = _resolve_metric_block(view, pid, base_period, metric)
    target_block = _resolve_metric_block(view, pid, target_period, metric)
    out.update(base=base_block, target=target_block)

    # 可比性核对：任一期不可比/缺失/不适用/零分母 → 对应状态，不给该差值
    for side, block in (('base', base_block), ('target', target_block)):
        if block['state'] == ST_PERIOD_MISSING:
            return _finish_diff(out, ST_PERIOD_MISSING,
                                '%s 期缺该对象记录（period_missing）：不给差，不代 0' % side)
        if block['state'] == ST_FIELD_MISSING:
            return _finish_diff(out, ST_FIELD_MISSING,
                                '%s 期字段缺失（field_missing）：不给差，不代 0' % side)
        if block['state'] == ST_NOT_APPLICABLE:
            return _finish_diff(out, ST_NOT_APPLICABLE,
                                '%s 期该指标按合同不适用（not_applicable）：不给差' % side)
        if block['state'] == OBJ_RECORD_CONFLICT:
            return _finish_diff(out, OBJ_RECORD_CONFLICT,
                                '%s 期同对象同周期记录互相矛盾（record_conflict）：不给差' % side)

    b_len, t_len = base_block.get('period_length_days'), target_block.get('period_length_days')
    b_layer, t_layer = base_block.get('source_layer'), target_block.get('source_layer')
    if b_len != t_len or b_layer != t_layer or base_block.get('unit') != target_block.get('unit'):
        return _finish_diff(out, ST_WINDOW_INCOMPARABLE,
                            '两期周期长度/来源层/单位不一致（window_incomparable：'
                            '长度 %s≠%s 或 来源层/单位不同）：不给差' % (b_len, t_len))

    b_val, t_val = base_block.get('value'), target_block.get('value')
    if not _is_num(b_val) or not _is_num(t_val):
        # 仍无值（如 /零分母）→ 归零分母，除非两侧都是缺失
        reason = ST_ZERO_DENOM if ST_ZERO_DENOM in (base_block['state'], target_block['state']) \
            else ST_FIELD_MISSING
        return _finish_diff(out, reason, '至少一期无可比值（%s）：不给差，不代 0' % reason)

    abs_diff = _round_num(t_val - b_val)
    out['absolute_diff'] = abs_diff
    if b_val == 0:
        out['percent_diff'] = 'unknown(zero_denominator)'
        out['percent_diff_value'] = None
        return _finish_diff(out, ST_ZERO_DENOM,
                            '基期值=0：绝对差仍给，百分比差=unknown(zero_denominator)，不写 0',
                            keep_amounts=True)
    out['percent_diff'] = _round_num((t_val - b_val) / b_val * 100.0)
    out['percent_diff_value'] = out['percent_diff']
    return _finish_diff(out, ST_OK, '两期同对象同指标同周期长度同口径且均有值：给绝对差与百分比差')


def _finish_diff(out, state, note, keep_amounts=False):
    out['state'] = state
    out['state_note'] = note
    if state != ST_OK:
        if not keep_amounts:
            out['absolute_diff'] = 'unknown(%s)' % state
            out['percent_diff'] = 'unknown(%s)' % state
        out['unknown_reason'] = state
    out.setdefault('absolute_diff', None)
    out.setdefault('percent_diff', None)
    if _is_num(out['absolute_diff']):
        out['direction'] = ('up' if out['absolute_diff'] > 0 else
                            'down' if out['absolute_diff'] < 0 else 'flat')
    return out


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _diff_set_summary(diffs):
    by_product = {}
    for item in diffs:
        by_product.setdefault(str(item['product_id']), item)

    direction_counts = {'up_count': 0, 'down_count': 0, 'flat_count': 0}
    absolute_diff_comparable_count = 0
    absolute_diff_unavailable_by_state = {}
    percent_diff_available_count = 0
    percent_diff_unavailable_by_state = {}

    for product_id in sorted(by_product):
        item = by_product[product_id]
        absolute_diff = item.get('absolute_diff')
        if _is_num(absolute_diff):
            absolute_diff_comparable_count += 1
            if absolute_diff > 0:
                direction_counts['up_count'] += 1
            elif absolute_diff < 0:
                direction_counts['down_count'] += 1
            else:
                direction_counts['flat_count'] += 1
        else:
            state = item['state']
            absolute_diff_unavailable_by_state[state] = absolute_diff_unavailable_by_state.get(state, 0) + 1

        if _is_num(item.get('percent_diff')):
            percent_diff_available_count += 1
        else:
            state = item['unknown_reason'] if item.get('unknown_reason') else item['state']
            percent_diff_unavailable_by_state[state] = percent_diff_unavailable_by_state.get(state, 0) + 1

    return {
        'unique_product_count': len(by_product),
        'direction_counts': direction_counts,
        'absolute_diff_comparable_count': absolute_diff_comparable_count,
        'absolute_diff_unavailable_by_state': dict(sorted(absolute_diff_unavailable_by_state.items())),
        'percent_diff_available_count': percent_diff_available_count,
        'percent_diff_unavailable_by_state': dict(sorted(percent_diff_unavailable_by_state.items())),
    }


# 多指标 metrics 与单指标 metric 互斥。


def _resolve_diff_metrics(req):
    """diff 指标解析：`metrics[]` 与旧 `metric` 互斥二选一 → 指标列表（请求顺序，不去重）。"""
    has_metrics = 'metrics' in req
    has_metric = 'metric' in req
    _require(not (has_metrics and has_metric), 'metrics_conflict',
             'diff 的 `metrics[]` 与旧 `metric` 互斥：只给一种（多指标用 metrics[]，'
             '单指标旧写法用 metric）')
    _require(has_metrics or has_metric, 'missing_metrics',
             'diff 须给 `metrics[]`（可多指标）或旧 `metric`（单指标）之一')
    if not has_metrics:
        return [_validate_metric(req.get('metric'))]
    raw = req.get('metrics')
    _require(isinstance(raw, list), 'invalid_structure', 'request.metrics 必须是数组')
    _require(bool(raw), 'missing_metrics', 'request.metrics 不可为空（无法定义比较对象）')
    seen, dups = set(), []
    for item in raw:
        key = str(item)
        if key in seen and key not in dups:
            dups.append(key)
        seen.add(key)
    _require(not dups, 'duplicate_metric',
             'request.metrics 重复（不静默去重）: %s' % ','.join(dups))
    return [_validate_metric(m) for m in raw]


def _diff_metric_block(diffs, view, metric):
    """单个指标的 diff 结果块：`results` + `summary`；M1/M2 多商品时附品类合计。"""
    summary = _diff_set_summary(diffs)
    totals = _category_totals(diffs, view, metric)
    if totals is not None:
        summary['category_totals'] = totals
    return {'metric': metric, 'metric_name': METRICS[metric]['name'],
            'unit': METRICS[metric]['unit'], **_metric_semantics(metric),
            'results': diffs, 'summary': summary}


def period_diff_multi(product_ids, metrics, base_period=None, target_period=None,
                       view=None, scope_resolution=None):
    """多指标跨期比较：`by_metric` 信封。

    同一范围、同一基期/目标期下逐指标给两期值、绝对差、百分比差、方向与可比状态；每个指标
    块与单指标 `period_diff_set` **同形同义**（同一 `period_diff` / `_diff_set_summary` /
    `_category_totals` 单点，不复制第二份计算）。指标之间不推断可比范围——某指标不可比的
    商品数与另一指标无关，不合成「整体可比/整体未知」。
    """
    view = view if view is not None else build_view()
    by_metric = {}
    for metric in metrics:
        diffs = [period_diff(pid, metric, base_period, target_period, view=view)
                 for pid in (product_ids or [])]
        _require(diffs, 'missing_objects', 'product_ids 须为非空数组')
        by_metric[metric] = _diff_metric_block(diffs, view, metric)
    out = {'event': 'period_diff_set', 'contract_version': PERIOD_CONTRACT_VERSION,
           'metrics_requested': list(metrics), 'by_metric': by_metric,
           'demo_caliber': PVG.DEMO_CALIBER}
    if scope_resolution is not None:
        out['scope_resolution'] = scope_resolution
    return out


AGGREGATE_METRICS = ('M1', 'M2')      # 可加总指标：订单量 / 播放量（商品层合计）
AGGREGATE_MIN_PRODUCTS = 2            # 少于两个唯一商品无聚合意义（不发新键）
# value 请求可选的聚合意图字段——缺省（不传）不发新键；显式传值才计算单期集合合计。
# 当前仅支持 'sum'（对 M1/M2 可加总指标求和）；其它值（'avg'/'count'/'min'/'max' 等）一律结构化拒绝。
AGGREGATE_VALUES = ('sum',)
_AGG_UNCATEGORIZED = '(uncategorized)'


def _is_comparable_diff(item):
    """该 diff 行是否可加总：两期值均为数值且未被身份/记录/窗口/零分母门禁拦下。

    与 `_diff_set_summary` 的 `absolute_diff_comparable_count` 同判据（absolute_diff 为数值）：
    `state=zero_denominator`（基期值=0）仍是可比——基期 0 是真值、绝对差已给，不代 0。
    period_missing / field_missing / identity_inconsistent / record_conflict / not_applicable /
    window_incomparable 一律不可比（不代 0、不进合计）。
    """
    return (_is_num(item.get('absolute_diff'))
            and _is_num((item.get('base') or {}).get('value'))
            and _is_num((item.get('target') or {}).get('value')))


def _category_totals(diffs, view, metric):
    """对本次选中商品的 M1/M2 跨期读数按目录品类分别汇总。

    仅两期均可比的商品参与合计，按唯一商品 ID 计数，并披露选中数、可比数和不可比原因。
    子集合计不代表完整目录品类；无可比商品时返回 unknown，基期合计为零时百分比不可用。"""
    if metric not in AGGREGATE_METRICS:
        return None
    by_product = {}
    requested_by_category = {}
    category_of = {str(o['product_id']): o.get('category') for o in view['objects']}
    for item in diffs:
        pid = str(item['product_id'])
        by_product.setdefault(pid, item)
        cat = _category_key(category_of.get(pid))
        requested_by_category[cat] = requested_by_category.get(cat, 0) + 1
    if len(by_product) < AGGREGATE_MIN_PRODUCTS:
        return None

    groups = {}
    order = []
    for obj in view['objects']:                    # 分组顺序＝目录稳定序，不跟随请求顺序
        cat = _category_key(obj.get('category'))
        if cat in requested_by_category and cat not in groups:
            groups[cat] = {'category': obj.get('category'),
                           'requested_product_count': requested_by_category[cat],
                           'coverage': {'selected_product_count': 0,
                                        'comparable_product_count': 0,
                                        'non_comparable_product_count': 0,
                                        'non_comparable_by_state': {}},
                           'base_sum': None, 'target_sum': None,
                           'absolute_change': None, 'percent_change': None}
            order.append(cat)

    sums = {cat: [0.0, 0.0] for cat in order}
    for pid in sorted(by_product):
        item = by_product[pid]
        cat = _category_key(category_of.get(pid))
        group = groups[cat]
        coverage = group['coverage']
        coverage['selected_product_count'] += 1
        if _is_comparable_diff(item):
            coverage['comparable_product_count'] += 1
            sums[cat][0] += item['base']['value']
            sums[cat][1] += item['target']['value']
        else:
            coverage['non_comparable_product_count'] += 1
            state = str(item.get('state') or 'unknown')
            by_state = coverage['non_comparable_by_state']
            by_state[state] = by_state.get(state, 0) + 1

    for cat in order:
        group = groups[cat]
        by_state = group['coverage']['non_comparable_by_state']
        group['coverage']['non_comparable_by_state'] = dict(sorted(by_state.items()))
        if not group['coverage']['comparable_product_count']:
            group['base_sum'] = None
            group['target_sum'] = None
            group['absolute_change'] = None
            group['percent_change'] = 'unknown(no_comparable_products)'
            continue
        base_sum, target_sum = sums[cat]
        group['base_sum'] = _round_num(base_sum)
        group['target_sum'] = _round_num(target_sum)
        group['absolute_change'] = _round_num(target_sum - base_sum)
        group['percent_change'] = (_round_num((target_sum - base_sum) / base_sum * 100.0)
                                   if base_sum else 'unknown(zero_denominator)')

    return {
        'group_by': 'catalog_category',
        'group_by_source': 'op=catalog 同一目录字段（period_view.objects[].category）',
        'sum_scope': 'comparable_products_only',
        'dedupe': 'unique_product_id',
        'note': '仅本次显式选中的商品按目录品类分组：合计只含两期均有可比读数的商品'
                '（基期值=0 仍计入、其百分比 unknown），不可比商品按状态分类披露、不代 0、'
                '不进合计；本聚合只代表所选商品，不代表完整目录品类总量；'
                'M1/M2 各自独立计算，可比范围可不同',
        'categories': [groups[cat] for cat in order],
    }


def single_period_totals(value_rows, metric, product_ids):
    """计算单期请求集合中 M1/M2 的合计，并披露成员覆盖。

    仅使用当前指标的 state=ok 数值行，按唯一商品 ID 去重；缺失不代零。
    存在缺失时标为已知可用值合计，全无效时返回 unknown(no_available_members)。
    不按品类拆组；不支持的指标或成员数不足时返回 None。"""
    if metric not in AGGREGATE_METRICS:
        return None
    if not isinstance(value_rows, list) or not value_rows:
        return None
    # 先按当前指标过滤交错结果，再按商品去重，避免不同指标串用数值。
    pids = sorted({str(pid) for pid in (product_ids or []) if pid})
    if len(pids) < AGGREGATE_MIN_PRODUCTS:
        return None
    rows = [r for r in value_rows
            if isinstance(r, dict) and str(r.get('metric') or '') == str(metric)]
    if not rows:
        return None

    # 按 product_id 去重（同 ID 多次请求不重复计入）；只含当前指标行
    per_pid = {}
    for r in rows:
        pid = str(r.get('product_id') or '')
        if pid:
            per_pid.setdefault(pid, r)
    total_sum = 0.0
    total_comparable = 0
    total_by_state = {}
    for pid in pids:
        row = per_pid[pid]
        val = row.get('value')
        state = str(row.get('state') or '')
        if state == ST_OK and _is_num(val):              # 仅 ok 数值参与（真 0 也有效）
            total_comparable += 1
            total_sum += val
        else:
            st = state if state else 'unknown'
            total_by_state[st] = total_by_state.get(st, 0) + 1

    total_selected = len(pids)
    total_non_comparable = total_selected - total_comparable

    # 全有效才称"完整"；全无效 → unknown（不显示为 0）
    if total_comparable == 0:
        value = 'unknown(no_available_members)'
        complete = False
    else:
        value = _round_num(total_sum)
        complete = (total_comparable == total_selected)

    return {
        'event': 'single_period_totals',
        'metric': metric,
        'metric_name': METRICS[metric]['name'],
        'unit': METRICS[metric]['unit'],
        'aggregate': 'sum',
        'sum_scope': ('comparable_products_only' if total_non_comparable
                      else 'all_selected_products'),
        'complete': complete,                            # 全请求成员该指标均有效才 True
        'sum': value,
        'selected_n': total_selected,
        'comparable_n': total_comparable,
        'non_comparable_n': total_non_comparable,
        'non_comparable_by_state': dict(sorted(total_by_state.items())),
        'product_ids': pids,
        'note': ('单期集合合计（本次请求集合 × 指标 × 单期时间）；只含可用值，'
                 '缺失/不可用不代 0；结果仅代表本次明确请求的成员集合；'
                 'M1/M2 各自独立计算，可比范围可不同'),
    }


# ===================== ：单期 value 的品类合计与成员贡献（只在 Data 计算） =====================

def _sum_blocks(blocks):
    """一组（商品 × 指标）读数块 → 合法合计与覆盖分类（缺失不代 0、真 0 有效）。"""
    total = 0.0
    comparable = 0
    by_state = {}
    for blk in blocks:
        state = str(blk.get('state') or '')
        val = blk.get('value')
        if state == ST_OK and _is_num(val):
            comparable += 1
            total += val
        else:
            key = state or 'unknown'
            by_state[key] = by_state.get(key, 0) + 1
    n = len(blocks)
    return {
        'sum': ('unknown(no_available_members)' if comparable == 0 else _round_num(total)),
        'complete': comparable == n and n > 0,
        'comparable_n': comparable,
        'non_comparable_n': n - comparable,
        'non_comparable_by_state': dict(sorted(by_state.items())),
        'member_count': n,
    }


def _value_category_totals(metrics, scope_members, block_for, view=None):
    """分别计算 M1/M2 的品类合计与完整品类贡献分母。

    分组来自商品目录，数值通过 block_for 读取同窗口同指标的权威读数。
    返回品类响应块与完整成员合计、覆盖信息，不重新定义指标公式。"""
    view = view if view is not None else build_view()
    scope_set = {str(p) for p in (scope_members or [])}
    cat_members, order = {}, []
    for obj in view['objects']:
        pid = str(obj['product_id'])
        cat = _category_key(obj.get('category'))
        if cat not in cat_members:
            cat_members[cat] = []
            order.append(cat)
        cat_members[cat].append(pid)
    touched = [cat for cat in order if any(pid in scope_set for pid in cat_members[cat])]

    totals, aggregates = {}, {}
    for metric in metrics:
        if metric not in AGGREGATE_METRICS:
            continue
        groups, per_cat = [], {}
        for cat in touched:
            full = cat_members[cat]
            selected = [pid for pid in full if pid in scope_set]
            memo = {}

            def _block(pid):
                if pid not in memo:
                    memo[pid] = block_for(pid, metric)
                return memo[pid]

            full_agg = _sum_blocks([_block(pid) for pid in full])
            selected_agg = (full_agg if len(selected) == len(full)
                            else _sum_blocks([_block(pid) for pid in selected]))
            member_scope = (CATEGORY_MEMBER_SCOPE_FULL if len(selected) == len(full)
                            else CATEGORY_MEMBER_SCOPE_SELECTED)
            entry = {
                'category': cat,
                'catalog_member_count': len(full),
                'member_count': len(selected),
                'coverage_complete': len(selected) == len(full),
                'complete': selected_agg['complete'],
                'sum': selected_agg['sum'],
                'selected_n': len(selected),
                'comparable_n': selected_agg['comparable_n'],
                'non_comparable_n': selected_agg['non_comparable_n'],
                'non_comparable_by_state': dict(selected_agg['non_comparable_by_state']),
                'member_scope': member_scope,
                'ref_key': ('%s:%s:%s' % (
                    'category_selected_total' if member_scope == CATEGORY_MEMBER_SCOPE_SELECTED
                    else 'category_total', metric, cat)),
            }
            if member_scope == CATEGORY_MEMBER_SCOPE_SELECTED:
                entry['full_category_total'] = {
                    'sum': full_agg['sum'],
                    'complete': full_agg['complete'],
                    'comparable_n': full_agg['comparable_n'],
                    'non_comparable_n': full_agg['non_comparable_n'],
                    'non_comparable_by_state': dict(full_agg['non_comparable_by_state']),
                    'ref_key': 'category_total:%s:%s' % (metric, cat),
                }
            groups.append(entry)
            per_cat[cat] = {'full': full_agg,
                            'entry': entry,
                            'denominator_ref_key': 'category_total:%s:%s' % (metric, cat)}
        totals[metric] = {
            'group_by': 'catalog_category',
            'group_by_source': 'period_view_generator.PRODUCTS[].category',
            'dedupe': 'unique_product_id',
            'note': ('分组合计只含本次范围中该品类的成员；member_scope=full_category 表示成员'
                     '等于目录完整品类；comparable/不可比按真实读数分类，缺失不代 0；'
                     'M1/M2 各自独立计算，可比范围可不同'),
            'categories': groups,
        }
        aggregates[metric] = per_cat
    return totals, aggregates


def _attach_value_contributions(rows, aggregates, view):
    """为成员读数附加其占同品类完整合计的比例。

    分母不完整、为零或成员不可用时只返回 share_state，不生成 share_percent。
    分子沿用成员引用，分母引用完整品类合计，不为贡献度另签证据。"""
    if not isinstance(rows, list) or not aggregates:
        return rows
    cat_of = {}
    for obj in view['objects']:
        cat_of[str(obj['product_id'])] = _category_key(obj.get('category'))
    for row in rows:
        if not isinstance(row, dict):
            continue
        metric = str(row.get('metric') or '')
        per_cat = aggregates.get(metric)
        if not per_cat:
            continue
        pid = str(row.get('product_id') or '')
        cat = cat_of.get(pid)
        agg = (per_cat or {}).get(cat)
        if agg is None:
            continue
        full = agg['full']
        contribution = {
            'metric': metric,
            'category': cat,
            'denominator': {
                'value': full['sum'],
                'complete': full['complete'],
                'member_scope': CATEGORY_MEMBER_SCOPE_FULL,
                'member_count': full['member_count'],
                'valid_member_count': full['comparable_n'],
            },
            'denominator_ref_key': agg['denominator_ref_key'],
        }
        value = row.get('value')
        if row.get('state') != ST_OK or not _is_num(value):
            contribution['share_state'] = 'unknown(member_unavailable)'
        elif not full['complete']:
            contribution['share_state'] = 'unknown(denominator_incomplete)'
            contribution['numerator'] = value
        elif not _is_num(full['sum']) or full['sum'] == 0:
            contribution['share_state'] = 'unknown(denominator_zero)'
            contribution['numerator'] = value
        else:
            contribution['share_state'] = 'ok'
            contribution['numerator'] = value
            contribution['share_percent'] = _round_num(value / full['sum'] * 100.0)
        row['contribution'] = contribution
    return rows


def category_totals_from_value_pairs(base_rows, target_rows, metric, objects):
    """将同集合、同指标的两次 value 读数按商品配对，复用品类跨期合计规则。

    只支持 M1/M2，覆盖、可比性与缺失处理均与 diff 路径一致；无法形成有效汇总时返回 None。"""
    if metric not in AGGREGATE_METRICS:
        return None
    base_by_pid = {str(r.get('product_id') or ''): r for r in (base_rows or [])
                   if isinstance(r, dict)}
    target_by_pid = {str(r.get('product_id') or ''): r for r in (target_rows or [])
                     if isinstance(r, dict)}
    pids = sorted(set(base_by_pid) & set(target_by_pid))
    if len(pids) < AGGREGATE_MIN_PRODUCTS:
        return None
    diffs = []
    for pid in pids:
        base, target = base_by_pid[pid], target_by_pid[pid]
        bv, tv = base.get('value'), target.get('value')
        comparable = _is_num(bv) and _is_num(tv)
        state = 'ok' if comparable else (
            str(target.get('state') or '') if str(target.get('state') or '') != 'ok'
            else (str(base.get('state') or '') or 'unknown'))
        diffs.append({
            'product_id': pid,
            'base': {'value': bv}, 'target': {'value': tv},
            'absolute_diff': _round_num(tv - bv) if comparable else None,
            'state': state,
        })
    return _category_totals(diffs, {'objects': objects or []}, metric)


def _category_key(category):
    """分组键：目录 category 原值；缺失/空值归入稳定占位键（不静默并进某个真实品类）。"""
    return str(category) if isinstance(category, str) and category else _AGG_UNCATEGORIZED


def _resolve_metric_block(view, pid, period_id, metric):
    recs = _view_records(view, [pid], period_id)
    if not recs:
        block = {'metric': metric, 'metric_name': METRICS[metric]['name'], 'unit': METRICS[metric]['unit'],
                 'product_id': pid, 'period_id': period_id, 'period_length_days': None,
                 **_metric_semantics(metric), 'demo_caliber': PVG.DEMO_CALIBER}
        return _unknown(block, 'period_missing', ST_PERIOD_MISSING)
    if len(recs) > 1 and len({_record_fingerprint(r) for r in recs}) > 1:
        return _unknown({**_base(recs[0], metric), 'record_ids': [r['record_id'] for r in recs]},
                        'record_conflict', OBJ_RECORD_CONFLICT)
    return metric_value(recs[0], metric)


# ===================== E4：周期内集合比较（排名 / 统计基准=中位数） =====================

def period_set_compare(product_ids, period_id, metric=_DEFAULT_SET_METRIC, kind='median', view=None,
                       scope_resolution=None):
    """在指定集合内按同周期、同指标计算中位数分组或排名。

    不可用对象进入缺失组，不参与基准计算；比较范围不扩大到未请求商品。"""
    view = view if view is not None else build_view()
    period_id = _validate_period_id(period_id)
    metric = _validate_metric(metric)
    _require(kind in ('median', 'rank'), 'unknown_compare_kind', 'kind 须为 median/rank')
    pids = sorted({str(p) for p in (product_ids or [])})
    _require(pids, 'missing_objects', 'product_ids 须为非空数组')
    known = {str(o['product_id']) for o in view['objects']}
    unknown_ids = sorted(set(pids) - known)
    _require(not unknown_ids, 'unknown_object', '对象不在本视图目录: %s' % ','.join(unknown_ids))

    usable, missing = [], []
    for pid in pids:
        block = _resolve_metric_block(view, pid, period_id, metric)
        if block['state'] == ST_OK and _is_num(block.get('value')):
            usable.append({'product_id': pid, 'value': block['value'],
                           'denominator': block.get('denominator')})
        else:
            missing.append({'product_id': pid, 'state': block['state'],
                            'value': block.get('value'), 'reason': block.get('unknown_reason')})

    out = {'event': 'period_set_compare', 'contract_version': PERIOD_CONTRACT_VERSION,
           'kind': kind, 'metric': metric, 'metric_name': METRICS[metric]['name'],
           'unit': METRICS[metric]['unit'], **_metric_semantics(metric),
           'period': dict(PVG.period_by_id(period_id)),
           'objects': pids, 'usable_count': len(usable), 'missing_count': len(missing),
           'missing_group': missing,
           'benchmark_disclosure': '本比较=同集合（本次显式选中 %d 个对象）在本周期的同域相对位置，'
                                   '不是行业基准' % len(pids),
           'demo_caliber': PVG.DEMO_CALIBER}
    if scope_resolution is not None:
        out['scope_resolution'] = scope_resolution
    return _finish_set_compare(out, usable, kind)


def _finish_set_compare(out, usable, kind):
    """rank / median 分组装配（周期窗口与日期窗口共用同一口径单点，不复制第二份）。"""
    if kind == 'rank':
        ordered = sorted(usable, key=lambda x: (-x['value'], x['product_id']))
        out['ranking'] = [{'rank': i + 1, 'product_id': x['product_id'], 'value': x['value']}
                          for i, x in enumerate(ordered)]
        return out

    # median：仅可比值参与；缺失/零分母不进比较
    if not usable:
        out.update(median=None, median_state='unknown(no_usable_values)',
                   high_group=[], equal_group=[], low_group=[],
                   median_rule='同集合水平=所选集合本周期该指标的中位数；无可用值→不产出分组')
        return out
    vals = sorted(x['value'] for x in usable)
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    med = _round_num(med)
    high = [x['product_id'] for x in usable if x['value'] > med]
    equal = [x['product_id'] for x in usable if x['value'] == med]
    low = [x['product_id'] for x in usable if x['value'] < med]
    out.update(median=med, median_state=ST_OK,
               high_group=sorted(high), equal_group=sorted(equal), low_group=sorted(low),
               median_rule='同集合水平=所选集合本周期该指标中位数；高于=值>中位数、低于=值<中位数、'
                           '等于中位数单列持平；无值/分母 0 不进比较，直接进缺失组')
    return out


# ===================== ：确定性组合条件筛选（cohort_filter） =====================

# 筛选与取值共用 resolve_scope 的范围定义，避免成员集合口径分叉。
COHORT_SCOPE_KINDS = SCOPE_KINDS
COHORT_FIELD_SINGLE = 'value'                       # 单期唯一字段
COHORT_FIELDS_PAIR = ('absolute_diff', 'percent_diff')   # 双期字段（同一期间对可混用）
COHORT_CONDITION_KEYS = ('metric', 'field', 'operator', 'value')
COHORT_OPERATORS = ('<', '<=', '==', '!=', '>=', '>')
COHORT_MAX_CONDITIONS = 4
COHORT_MAX_SCOPE_IDS = 60        # 超出集合规模限制时拒绝，不静默截断。
_COHORT_FIELDS_ALL = (COHORT_FIELD_SINGLE,) + COHORT_FIELDS_PAIR
_COHORT_COMPARE = {'<': _operator.lt, '<=': _operator.le, '==': _operator.eq,
                   '!=': _operator.ne, '>=': _operator.ge, '>': _operator.gt}


def _is_finite_num(v):
    """有限数值（排除 bool、NaN、±Infinity；_is_num 已排除 bool）。"""
    return _is_num(v) and math.isfinite(v)


def _parse_cohort_scope(scope, view, allowed_product_ids=None):
    """通过 resolve_scope 解析筛选范围，与取值操作共用范围语义和错误码。

范围支持全池、目录品类或显式商品 ID；返回规范化范围与成员集合。"""
    resolved = resolve_scope(scope, view=view, allowed_product_ids=allowed_product_ids)
    return resolved['normalized'], resolved['members']


def _parse_cohort_conditions(conditions, mode):
    """校验并规范化 1–4 条平面 AND 条件（closed-schema；阈值必须为有限数值）。"""
    _require(isinstance(conditions, list), 'invalid_structure', 'conditions 必须是数组')
    _require(bool(conditions), 'missing_conditions',
             'conditions 须为 1–%d 条' % COHORT_MAX_CONDITIONS)
    _require(len(conditions) <= COHORT_MAX_CONDITIONS, 'too_many_conditions',
             'conditions %d 条超过上限 %d（平面 AND，无嵌套/OR/NOT）'
             % (len(conditions), COHORT_MAX_CONDITIONS))
    allowed_fields = (COHORT_FIELD_SINGLE,) if mode == 'single' else COHORT_FIELDS_PAIR
    parsed = []
    for i, cond in enumerate(conditions):
        _require(isinstance(cond, dict), 'invalid_structure',
                 'conditions[%d] 必须是 JSON 对象' % i)
        extra = sorted(set(cond.keys()) - set(COHORT_CONDITION_KEYS))
        _require(not extra, 'unknown_keys',
                 'conditions[%d] 未知键: %s' % (i, ','.join(extra)))
        missing = sorted(set(COHORT_CONDITION_KEYS) - set(cond.keys()))
        _require(not missing, 'missing_condition_key',
                 'conditions[%d] 缺键: %s' % (i, ','.join(missing)))
        metric = _validate_metric(cond['metric'])
        field = cond['field']
        if field not in allowed_fields:
            code = ('unknown_field' if field not in _COHORT_FIELDS_ALL
                    else 'field_period_mode_mismatch')
            _require(False, code,
                     'conditions[%d].field=%r 在当前时间模式下不合法（允许 %s）'
                     % (i, field, '/'.join(allowed_fields)))
        _require(cond['operator'] in COHORT_OPERATORS, 'unknown_operator',
                 'conditions[%d].operator=%r 不在闭集 %s'
                 % (i, cond['operator'], ' '.join(COHORT_OPERATORS)))
        _require(_is_finite_num(cond['value']), 'invalid_threshold',
                 'conditions[%d].value=%r 须为有限数值（bool/NaN/Infinity 均拒绝）'
                 % (i, cond['value']))
        parsed.append({'metric': metric, 'field': field,
                       'operator': cond['operator'], 'value': cond['value']})
    return parsed


def _cohort_eval_operand(view, pid, mode, metric, field,
                         period_id, base_period_id, target_period_id):
    """单个（商品 × 条件）操作数求值：返回 (数值, None) 或 (None, unknown 原因)。

    判定必须落到被请求字段：单期走 `_resolve_metric_block`（period_missing/record_conflict/
    field_missing/zero_denominator 分别成 unknown）；双期复用 `period_diff` 单点——基期=0 时
    absolute_diff 仍可判，percent_diff=unknown(zero_denominator)；identity_inconsistent/
    record_conflict/period_missing 等整段不给差值的字段按现有原因成 unknown。"""
    if mode == 'single':
        block = _resolve_metric_block(view, pid, period_id, metric)
        if block['state'] == ST_OK and _is_num(block.get('value')):
            return block['value'], None
        return None, block.get('unknown_reason') or block['state']
    diff_out = period_diff(pid, metric, base_period_id, target_period_id, view=view)
    raw = diff_out.get(field)          # 只看被请求字段，不用整行 state 一刀切
    if _is_num(raw):
        return raw, None
    return None, diff_out.get('unknown_reason') or 'not_determinable'


def period_cohort_filter(scope, conditions, period_id=None, base_period_id=None,
                         target_period_id=None, view=None, allowed_product_ids=None):
    """ 确定性组合条件筛选：同一 scope 逐商品按 1–4 条平面 AND 条件求交。

    时间模式二选一：单期 `period_id` + field=value；双期 `base_period_id`+`target_period_id`
    + field=absolute_diff/percent_diff（同一期间对可混用两种字段）。三值 AND：任一 false→
    nonmatch（false AND unknown=false）；全部 true→match；否则 unknown（成员保留条件级原因）。
    结果覆盖全部 scope：scope_count = match_count + nonmatch_count + predicate_unknown_count；
    完整返回 matches 与 unknown 成员（v1 不分页、不截断、不排名，目录稳定顺序）。"""
    view = view if view is not None else build_view()
    # 范围解析走共享单点 `resolve_scope`（与 `value` 同一套形态与错误码），
    # 因此 `cohort_filter` 也支持 `category`（一个或多个目录品类），并随响应给出
    # `scope_resolution`（实际成员/覆盖/根任务商品上界）。判定逻辑本身不重写。
    resolved_scope = resolve_scope(scope, view=view, allowed_product_ids=allowed_product_ids)
    scope_norm, scope_ids = resolved_scope['normalized'], resolved_scope['members']
    scope_resolution = resolved_scope['resolution']

    if period_id is not None:
        _require(base_period_id is None and target_period_id is None, 'period_mode_conflict',
                 'cohort_filter 单期 period_id 与双期 base/target_period_id 不可同时出现'
                 '（时间模式由周期键唯一确定）')
        period_id = _validate_period_id(period_id)
        mode = 'single'
    else:
        _require(base_period_id is not None or target_period_id is not None, 'missing_period_id',
                 'cohort_filter 须给单期 period_id 或成对 base_period_id+target_period_id')
        _require(base_period_id is not None and target_period_id is not None,
                 'period_pair_incomplete',
                 '双期条件须同时给 base_period_id 和 target_period_id，不可只给其一')
        base_period_id = _validate_period_id(base_period_id)
        target_period_id = _validate_period_id(target_period_id)
        mode = 'pair'

    parsed_conditions = _parse_cohort_conditions(conditions, mode)

    matches, unknowns, nonmatch_count = [], [], 0
    for pid in scope_ids:
        cells, verdicts = {}, []
        for cond in parsed_conditions:
            key = '%s_%s' % (cond['metric'], cond['field'])
            value, reason = _cohort_eval_operand(view, pid, mode, cond['metric'], cond['field'],
                                                 period_id, base_period_id, target_period_id)
            if reason is None:
                cells[key] = value
                verdicts.append(_COHORT_COMPARE[cond['operator']](value, cond['value']))
            else:
                cells[key] = 'unknown(%s)' % reason
                verdicts.append(None)
        if any(v is False for v in verdicts):
            nonmatch_count += 1                    # false AND unknown = false
        elif all(v is True for v in verdicts):
            matches.append({'product_id': pid, **cells})
        else:
            unknowns.append({'product_id': pid, **cells})

    scope_count = len(scope_ids)
    out = {'event': 'period_cohort_filter', 'contract_version': PERIOD_CONTRACT_VERSION,
           'view_version': view['view_version'],
           'scope': scope_norm,
           'scope_resolution': scope_resolution,
           'conditions': parsed_conditions,
           'time_mode': 'single_period' if mode == 'single' else 'period_pair',
           'ordered_by': 'catalog',
           'scope_count': scope_count,
           'match_count': len(matches),
           'nonmatch_count': nonmatch_count,
           'predicate_unknown_count': len(unknowns),
           # 已确认命中占比：match_count/scope_count×100。unknown 存在时它不是全池精确满足率
           #（分母语义由 share_denominator 声明），不得改口述为真实满足率。
           'confirmed_share_percent': _round_num(len(matches) / scope_count * 100.0),
           'share_denominator': 'scope_count',
           # 唯一完整性标志：全部 match 与 unknown 成员已在响应中完整返回（v1 无分页/无截断；
           # 成员列表长度必须分别等于 match_count / predicate_unknown_count 供下游核验）。
           'members_complete': True,
           'matches': matches,
           'unknown': unknowns,
           'demo_caliber': PVG.DEMO_CALIBER}
    if mode == 'single':
        out['period_id'] = period_id
    else:
        out['base_period_id'] = base_period_id
        out['target_period_id'] = target_period_id
    return out


# ===================== 对象 / 记录状态（ B） =====================

def object_states(product_id, view=None, base_period_id=None, target_period_id=None):
    """区分稳定身份错误、正常状态变化、分类属性漂移与周期内记录冲突。

    跨期取值不同本身不构成记录冲突；显式指定周期对时只检查该两期。"""
    _require((base_period_id is None) == (target_period_id is None), 'missing_state_window',
             'states 显式周期窗口须同时给 base_period_id 和 target_period_id')
    explicit_window = base_period_id is not None
    if explicit_window:
        base_period_id = _validate_period_id(base_period_id)
        target_period_id = _validate_period_id(target_period_id)

    view = view if view is not None else build_view()
    pid = str(product_id)
    all_recs = [r for r in view['records'] if str(r['product_id']) == pid]
    if explicit_window:
        known = {str(obj['product_id']) for obj in view['objects']}
        _require(pid in known, 'unknown_object', '对象 %r 不在本视图目录' % pid)
        window_periods = {base_period_id, target_period_id}
        recs = [r for r in all_recs if r['period_id'] in window_periods]
    else:
        recs = all_recs
        _require(recs, 'unknown_object', '对象 %r 不在本视图目录' % pid)

    out = {'event': 'object_states', 'contract_version': PERIOD_CONTRACT_VERSION,
           'product_id': pid, 'identity': {}, 'state_changes': [], 'attribute_drift': [],
           'record_conflicts': [], 'states': [], 'demo_caliber': PVG.DEMO_CALIBER}

    by_period = {}
    for r in recs:
        by_period.setdefault(r['period_id'], []).append(r)

    # 显式周期对只检查这两期；未给周期对时检查完整历史。
    names = sorted({r.get('product_name') for r in recs})
    if not names:
        out['identity'] = {'product_id': pid, 'product_name': None, 'state': ST_PERIOD_MISSING}
        out['states'].append(ST_PERIOD_MISSING)
    else:
        out['identity'] = {'product_id': pid, 'product_name': names[0] if len(names) == 1 else names}
    if len(names) > 1:
        out['identity']['state'] = OBJ_IDENTITY_INCONSISTENT
        out['states'].append(OBJ_IDENTITY_INCONSISTENT)
    elif names:
        out['identity']['state'] = ST_OK

    # 同周期记录冲突：
    for period_id in sorted(by_period):
        group = by_period[period_id]
        if len(group) > 1 and len({_record_fingerprint(r) for r in group}) > 1:
            out['record_conflicts'].append({
                'period_id': period_id, 'record_ids': sorted(r['record_id'] for r in group),
                'note': '同一对象同一周期内互相矛盾的记录（§4.2/§4.4B）：列各记录，标数据冲突'})
    if out['record_conflicts']:
        out['states'].append(OBJ_RECORD_CONFLICT)

    # 默认沿用首末周期；显式请求则严格使用给定窗口。
    periods = [period['period_id'] for period in PR.allowed_periods_in_time_order(by_period)]
    if explicit_window:
        base_period, target_period = base_period_id, target_period_id
    elif len(periods) >= 2:
        base_period, target_period = periods[0], periods[-1]
    else:
        base_period = target_period = None

    window_state = ST_OK
    if base_period is None or target_period is None \
            or base_period not in by_period or target_period not in by_period:
        window_state = ST_PERIOD_MISSING
    else:
        conflicting_periods = {c['period_id'] for c in out['record_conflicts']}
        if base_period in conflicting_periods or target_period in conflicting_periods:
            window_state = OBJ_RECORD_CONFLICT
        elif not explicit_window and out['record_conflicts']:
            # Preserve the legacy all-period window behavior: any conflict suppresses comparison.
            window_state = OBJ_RECORD_CONFLICT
    if explicit_window and window_state == ST_PERIOD_MISSING \
            and ST_PERIOD_MISSING not in out['states']:
        out['states'].append(ST_PERIOD_MISSING)
    out['comparison_window'] = {
        'base_period_id': base_period,
        'target_period_id': target_period,
        'state': window_state,
    }

    # 跨期状态/属性变化（只在所选两期都有可比记录时比较）：
    if window_state == ST_OK:
        first, last = by_period[base_period][0], by_period[target_period][0]
        for fld, code in (('published', OBJ_STATE_CHANGE), ('price', OBJ_STATE_CHANGE),
                          ('category', OBJ_ATTRIBUTE_DRIFT), ('city', OBJ_ATTRIBUTE_DRIFT)):
            if first.get(fld) != last.get(fld):
                item = {'field': fld, 'base_period_id': base_period, 'base_value': first.get(fld),
                        'target_period_id': target_period, 'target_value': last.get(fld)}
                if code == OBJ_STATE_CHANGE:
                    item['note'] = '周期状态字段跨期不同=正常变化（state_change），不算冲突（§4.2）'
                    out['state_changes'].append(item)
                else:
                    item['note'] = '分类属性跨期不同=待复核（attribute_drift），不静默择一（§4.2）'
                    out['attribute_drift'].append(item)
        if out['state_changes']:
            out['states'].append(OBJ_STATE_CHANGE)
        if out['attribute_drift']:
            out['states'].append(OBJ_ATTRIBUTE_DRIFT)
    return out


# 日期窗口取数与比较使用 daily_aggregate；按日累加分子和分母后计算比率。

def _validate_window(window, role='window'):
    """显式日期窗口校验 → (start_iso, end_iso)。覆盖与顺序校验复用 daily_aggregate 单点。"""
    _require(isinstance(window, dict), 'invalid_structure', '%s 必须是 JSON 对象' % role)
    extra = sorted(set(window.keys()) - {'start_date', 'end_date'})
    _require(not extra, 'unknown_keys', '%s 未知键: %s' % (role, ','.join(extra)))
    missing = sorted({'start_date', 'end_date'} - set(window.keys()))
    _require(not missing, 'missing_window_keys', '%s 缺键: %s' % (role, ','.join(missing)))
    dates = DAILY._validated_range(window['start_date'], window['end_date'])
    return dates[0], dates[-1]


def _window_identity(start, end, dates):
    """日期窗口身份块（真实起止日期 + 粒度 + 覆盖天数；查询结果据此可回查真实时间范围）。"""
    return {'start_date': start, 'end_date': end, 'granularity': PVG.DAILY_GRANULARITY,
            'n_days': len(dates), 'dates': list(dates)}


def _require_products(product_ids, view):
    """对象集合校验（目录内、非空、去重排序）——与 period_values 同纪律。"""
    pids = [str(p) for p in (product_ids or [])]
    _require(pids, 'missing_objects', 'product_ids 须为非空数组')
    known = {str(o['product_id']) for o in view['objects']}
    unknown_ids = sorted(set(pids) - known)
    _require(not unknown_ids, 'unknown_object', '对象不在本视图目录: %s' % ','.join(unknown_ids))
    return sorted(set(pids))


def window_values(product_ids, window, metrics, view=None, aggregate=None,
                  group_by=None, scope_resolution=None):
    """按真实日期窗口读取每对象每指标的范围值。

    aggregate=sum 对 M1/M2 生成集合合计；group_by=category 复用品类合计与贡献规则。
    所有结果披露真实起止日期，缺失不代零。"""
    view = view if view is not None else build_view()
    start, end = _validate_window(window)
    metrics = [_validate_metric(m) for m in metrics]
    _require(metrics, 'missing_metrics', 'metrics 须为非空数组')
    pids = _require_products(product_ids, view)
    dates = DAILY._validated_range(start, end)
    results = []
    for pid in pids:
        agg = DAILY.aggregate_range(pid, start, end, metrics, _dates=dates)
        results.extend(agg['results'])
    out = {'event': 'period_window_values', 'contract_version': PERIOD_CONTRACT_VERSION,
           'time_mode': 'date_window', 'window': _window_identity(start, end, dates),
           'metrics_requested': metrics, 'objects': pids,
           'results': results, 'demo_caliber': PVG.DEMO_CALIBER}
    if scope_resolution is not None:
        out['scope_resolution'] = scope_resolution
    if aggregate is not None:
        _require(aggregate in AGGREGATE_VALUES, 'aggregate_invalid',
                 'aggregate 仅支持 "sum"；当前=%r' % (aggregate,))
        spt = {}
        for m in metrics:
            if m not in AGGREGATE_METRICS:
                _require(False, 'aggregate_metric_not_supported',
                         '单期集合合计仅支持 M1（订单量）/ M2（播放量）；指标 %s 不支持 '
                         'aggregate=sum（M3/M4/M5 为比率或效率指标，不可跨商品直接求和）。'
                         '若需该指标的逐商品数值，改用不带 aggregate 的 value；'
                         '若需集合排名或中位数比较，改用 set_compare（kind=rank 或 kind=median）。'
                         % m)
            entry = single_period_totals(results, m, pids)
            if entry is not None:
                spt[m] = entry
        if spt:
            out['single_period_totals'] = spt
    _require_group_by(group_by, aggregate)
    if group_by is not None:
        def _block_for(pid, metric):
            return DAILY.aggregate_range(pid, start, end, [metric],
                                         _dates=dates)['results'][0]
        totals, aggregates = _value_category_totals(metrics, pids, _block_for, view)
        out['category_totals'] = totals
        _attach_value_contributions(results, aggregates, view)
    return out


def _window_identity_state(pid, base_window, target_window):
    """两个被比较日期窗口内的稳定身份：只取窗口内有效日事实（排除冲突日），窗口外历史不参与。"""
    index = DAILY.daily_index()
    names = set()
    for start, end in (base_window, target_window):
        for date_str in DAILY._validated_range(start, end):
            recs = index.get((pid, date_str)) or []
            if not recs or any(r.get('record_conflict') for r in recs):
                continue
            names.update(r.get('product_name') for r in recs)
    if not names:
        return {'product_id': pid, 'product_name': None, 'state': ST_PERIOD_MISSING}
    return {'product_id': pid,
            'product_name': next(iter(names)) if len(names) == 1 else sorted(names, key=str),
            'state': ST_OK if len(names) == 1 else OBJ_IDENTITY_INCONSISTENT}


def _window_diff_row(pid, metric, base_agg, target_agg, base_window, target_window, view=None):
    """计算两日期窗口的对象指标差值，先检查窗口内有效事实的稳定身份一致性。

    窗口外的名称变化不影响本次比较；可比性与错误状态沿用周期比较规则。"""
    bb, tb = base_agg['results'][0], target_agg['results'][0]
    out = {'event': 'period_window_diff', 'product_id': pid, 'metric': metric,
           'metric_name': METRICS[metric]['name'], 'unit': METRICS[metric]['unit'],
           **_metric_semantics(metric),
           'base': bb, 'target': tb,
           'base_window': _window_identity(base_window[0], base_window[1],
                                           DAILY._validated_range(base_window[0], base_window[1])),
           'target_window': _window_identity(target_window[0], target_window[1],
                                             DAILY._validated_range(target_window[0], target_window[1])),
           'absolute_diff_formula': '值_target − 值_base（同单位）',
           'percent_diff_formula': '(值_target − 值_base) / 值_base × 100%（仅当值_base≠0）',
           'demo_caliber': PVG.DEMO_CALIBER}
    # 身份门禁只取两个被比较日期窗口内的有效日事实：窗口外历史名称变化不阻断本次比较。
    identity = _window_identity_state(pid, base_window, target_window)
    if identity['state'] == OBJ_IDENTITY_INCONSISTENT:
        out['identity'] = identity
        return _finish_diff(out, OBJ_IDENTITY_INCONSISTENT,
                            '比较窗口内稳定身份字段不一致：停止该日期窗口对象比较，不计算差值'
                            '')
    b_len, t_len = bb.get('period_length_days'), tb.get('period_length_days')
    out['window_length_days'] = {'base': b_len, 'target': t_len}
    out['window_aligned'] = (b_len == t_len)
    out['window_alignment_note'] = (
        '两侧窗口等长（各 %d 天），直接可比' % b_len if out['window_aligned'] else
        '两侧窗口天数不同（base %s 天 / target %s 天）：差值按各窗口整体口径计算，'
        '不是同进度比较——报告时须写明两段真实起止日期与天数，不写成等长环比'
        % (b_len, t_len))
    for side, blk in (('base', bb), ('target', tb)):
        if blk['state'] == ST_PERIOD_MISSING:
            return _finish_diff(out, ST_PERIOD_MISSING,
                                '%s 窗口缺该对象日事实（period_missing）：不给差，不代 0' % side)
        if blk['state'] == ST_FIELD_MISSING:
            return _finish_diff(out, ST_FIELD_MISSING,
                                '%s 窗口字段缺失（field_missing）：不给差，不代 0' % side)
        if blk['state'] == OBJ_RECORD_CONFLICT:
            return _finish_diff(out, OBJ_RECORD_CONFLICT,
                                '%s 窗口含冲突日（record_conflict）：不给差' % side)
    b_val, t_val = bb.get('value'), tb.get('value')
    if not _is_num(b_val) or not _is_num(t_val):
        reason = ST_ZERO_DENOM if ST_ZERO_DENOM in (bb['state'], tb['state']) else ST_FIELD_MISSING
        return _finish_diff(out, reason, '至少一侧无可比值（%s）：不给差，不代 0' % reason)
    out['absolute_diff'] = _round_num(t_val - b_val)
    if b_val == 0:
        out['percent_diff'] = 'unknown(zero_denominator)'
        out['percent_diff_value'] = None
        return _finish_diff(out, ST_ZERO_DENOM,
                            '基期窗口值=0：绝对差仍给，百分比差=unknown(zero_denominator)，不写 0',
                            keep_amounts=True)
    out['percent_diff'] = _round_num((t_val - b_val) / b_val * 100.0)
    out['percent_diff_value'] = out['percent_diff']
    return _finish_diff(out, ST_OK, '两侧窗口同对象同指标均有值：给绝对差与百分比差')


def _prepare_window_pair(base_window, target_window):
    """日期窗口对的公共校验：两侧真实起止日期 + 方向（base 须早于 target）。"""
    bs, be = _validate_window(base_window, 'base_window')
    ts, te = _validate_window(target_window, 'target_window')
    _require(bs < ts, 'time_window_direction_invalid',
             '日期窗口方向不成立：base=%s~%s 不早于 target=%s~%s（base 须早于 target）'
             % (bs, be, ts, te))
    return bs, be, ts, te, DAILY._validated_range(bs, be), DAILY._validated_range(ts, te)


def _window_diff_rows(pids, metric, bs, be, ts, te, b_dates, t_dates, view):
    """单指标的日期窗口 diff 行 + 汇总（周期与多指标两条路径共用同一实现）。"""
    diffs = []
    for pid in pids:
        base_agg = DAILY.aggregate_range(pid, bs, be, [metric], _dates=b_dates)
        target_agg = DAILY.aggregate_range(pid, ts, te, [metric], _dates=t_dates)
        diffs.append(_window_diff_row(pid, metric, base_agg, target_agg, (bs, be), (ts, te),
                                      view=view))
    return diffs


def window_diff(product_ids, metric, base_window, target_window, view=None,
                scope_resolution=None):
    """日期窗口跨窗口差值（单日环比 / 上周同日 / 完整月 / 月内同进度皆走同一路径）。"""
    view = view if view is not None else build_view()
    bs, be, ts, te, b_dates, t_dates = _prepare_window_pair(base_window, target_window)
    metric = _validate_metric(metric)
    pids = _require_products(product_ids, view)
    diffs = _window_diff_rows(pids, metric, bs, be, ts, te, b_dates, t_dates, view)
    summary = _diff_set_summary(diffs)
    totals = _category_totals(diffs, view, metric)
    if totals is not None:
        summary['category_totals'] = totals
    out = {'event': 'period_window_diff_set', 'contract_version': PERIOD_CONTRACT_VERSION,
           'time_mode': 'date_window', 'metric': metric, **_metric_semantics(metric),
           'base_window': _window_identity(bs, be, b_dates),
           'target_window': _window_identity(ts, te, t_dates),
           'results': diffs, 'summary': summary, 'demo_caliber': PVG.DEMO_CALIBER}
    if scope_resolution is not None:
        out['scope_resolution'] = scope_resolution
    return out


def window_diff_multi(product_ids, metrics, base_window, target_window, view=None,
                      scope_resolution=None):
    """多指标日期窗口跨期比较：`by_metric` 信封，与周期多指标同形。

    每指标块与单指标 `period_window_diff_set` 同形同义（同一 `_window_diff_row` /
    `_diff_set_summary` / `_category_totals` 单点）；顶层保留两侧真实起止日期身份。
    """
    view = view if view is not None else build_view()
    bs, be, ts, te, b_dates, t_dates = _prepare_window_pair(base_window, target_window)
    pids = _require_products(product_ids, view)
    by_metric = {}
    for metric in metrics:
        diffs = _window_diff_rows(pids, metric, bs, be, ts, te, b_dates, t_dates, view)
        by_metric[metric] = _diff_metric_block(diffs, view, metric)
    out = {'event': 'period_window_diff_set', 'contract_version': PERIOD_CONTRACT_VERSION,
           'time_mode': 'date_window', 'metrics_requested': list(metrics),
           'by_metric': by_metric,
           'base_window': _window_identity(bs, be, b_dates),
           'target_window': _window_identity(ts, te, t_dates),
           'demo_caliber': PVG.DEMO_CALIBER}
    if scope_resolution is not None:
        out['scope_resolution'] = scope_resolution
    return out


def window_set_compare(product_ids, window, metric=_DEFAULT_SET_METRIC, kind='median', view=None,
                       scope_resolution=None):
    """日期窗口内同集合比较（排名 / 中位数基准）——口径单点复用 `_finish_set_compare`。"""
    view = view if view is not None else build_view()
    start, end = _validate_window(window)
    metric = _validate_metric(metric)
    _require(kind in ('median', 'rank'), 'unknown_compare_kind', 'kind 须为 median/rank')
    pids = _require_products(product_ids, view)
    dates = DAILY._validated_range(start, end)
    usable, missing = [], []
    for pid in pids:
        blk = DAILY.aggregate_range(pid, start, end, [metric], _dates=dates)['results'][0]
        if blk['state'] == ST_OK and _is_num(blk.get('value')):
            usable.append({'product_id': pid, 'value': blk['value'],
                           'denominator': blk.get('denominator')})
        else:
            missing.append({'product_id': pid, 'state': blk['state'],
                            'value': blk.get('value'), 'reason': blk.get('unknown_reason')})
    out = {'event': 'period_window_set_compare', 'contract_version': PERIOD_CONTRACT_VERSION,
           'time_mode': 'date_window', 'kind': kind, 'metric': metric,
           'metric_name': METRICS[metric]['name'], 'unit': METRICS[metric]['unit'],
           **_metric_semantics(metric),
           'window': _window_identity(start, end, dates),
           'objects': pids, 'usable_count': len(usable), 'missing_count': len(missing),
           'missing_group': missing,
           'benchmark_disclosure': '本比较=同集合（本次显式选中 %d 个对象）在该日期窗口内的同域'
                                   '相对位置，不是行业基准' % len(pids),
           'demo_caliber': PVG.DEMO_CALIBER}
    if scope_resolution is not None:
        out['scope_resolution'] = scope_resolution
    return _finish_set_compare(out, usable, kind)


def daily_series(product_ids, window, metrics, view=None):
    """日期窗口内的**连续日读数**（每对象每指标：逐日值序列 + 该窗口范围汇总块）。

    「最近 N 天哪些商品持续下滑」一次拿到连续日读数，不必每天逐次查数；分析哪些指标与
    商品仍由 Root 自主决定。逐日值与范围汇总走同一条 `daily_aggregate` 路径，不冲突。
    """
    view = view if view is not None else build_view()
    start, end = _validate_window(window)
    metrics = [_validate_metric(m) for m in metrics]
    _require(metrics, 'missing_metrics', 'metrics 须为非空数组')
    pids = _require_products(product_ids, view)
    dates = DAILY._validated_range(start, end)
    results = []
    for pid in pids:
        per_day = [DAILY.aggregate_range(pid, d, d, metrics, _dates=[d]) for d in dates]
        range_agg = DAILY.aggregate_range(pid, start, end, metrics, _dates=dates)
        for i, m in enumerate(metrics):
            days = []
            for k, d in enumerate(dates):
                blk = per_day[k]['results'][i]
                item = {'date': d, 'value': blk.get('value'), 'state': blk['state']}
                if blk.get('unknown_reason'):
                    item['unknown_reason'] = blk['unknown_reason']
                days.append(item)
            results.append({'product_id': pid, 'metric': m,
                            'range': range_agg['results'][i], 'days': days})
    return {'event': 'period_daily_series', 'contract_version': PERIOD_CONTRACT_VERSION,
            'time_mode': 'date_window', 'window': _window_identity(start, end, dates),
            'granularity': PVG.DAILY_GRANULARITY, 'metrics_requested': metrics,
            'objects': pids, 'results': results, 'demo_caliber': PVG.DEMO_CALIBER}


# ===================== 结构化解锁入口（pctx_v1；只读、无模型、无文件 IO） =====================

_OPS = QUERY_OPS
COHORT_REQUEST_KEYS = ('op', 'scope', 'conditions', 'period_id',
                       'base_period_id', 'target_period_id')

OP_REQUEST_KEYS = {
    # value 支持 scope 和品类分组；scope 与顶层 product_ids 互斥。
    'value': ('op', 'product_ids', 'scope', 'group_by', 'period_id', 'metrics', 'window',
              'aggregate'),
    'diff': ('op', 'product_ids', 'scope', 'metrics', 'metric', 'base_period_id', 'target_period_id',
             'base_window', 'target_window'),
    # set_compare 使用单指标 metric，未给时采用默认指标；范围写法保持互斥。
    'set_compare': ('op', 'product_ids', 'scope', 'period_id', 'metric', 'kind', 'window'),
    'states': ('op', 'product_ids', 'base_period_id', 'target_period_id'),
    'catalog': ('op',),
    COHORT_OP: COHORT_REQUEST_KEYS,
    # 日级连续读数必须显式给日期窗口（周期选择器不再决定日/月能力）。
    DAILY_SERIES_OP: ('op', 'product_ids', 'metrics', 'window'),
}


def validate_period_query_envelope(payload):
    """外层版本合同单点校验；无效时返回结构化拒绝，有效时返回 None。"""
    try:
        _require(isinstance(payload, dict), 'invalid_structure', '请求必须是 JSON 对象')
        _require(payload.get('contract_version') == PERIOD_CONTRACT_VERSION,
                 'contract_version_unsupported',
                 'contract_version=%r（当前 %s）' % (payload.get('contract_version'),
                                                       PERIOD_CONTRACT_VERSION))
        unknown = sorted(set(payload.keys()) - {'contract_version', 'request'})
        _require(not unknown, 'unknown_keys', '未知顶层键: %s' % ','.join(unknown))
    except QueryRejected as e:
        return {'event': 'period_rejected', 'error_code': e.code, 'detail': e.detail,
                'request_echo': None, 'demo_caliber': PVG.DEMO_CALIBER}
    return None


def execute_period_query(payload, run_state=None, allowed_product_ids=None):
    """执行只读查询，未知键、命名空间混用或非法指标返回结构化 period_rejected。

    allowed_product_ids 限定 benchmark_all 的解析范围；显式越界请求由根任务守卫拒绝。"""
    envelope_rejection = validate_period_query_envelope(payload)
    if envelope_rejection is not None:
        return envelope_rejection
    cq = None
    try:
        req = payload.get('request')
        _require(isinstance(req, dict), 'invalid_structure', 'request 必须是 JSON 对象')
        op = req.get('op')
        _require(op in _OPS, 'unknown_op', 'op=%r 不在 %s' % (op, '/'.join(_OPS)))
        cq = req
        allowed_keys = OP_REQUEST_KEYS[op]
        unknown_r = sorted(set(req.keys()) - set(allowed_keys))
        _require(not unknown_r, 'unknown_keys', '未知 request 键: %s' % ','.join(unknown_r))
        # 查询使用固定种子的模拟视图，调用方不能通过更换 seed 改变同一问题的数据。
        view = build_view()

        if op == 'catalog':
            return {'event': 'period_catalog', 'contract_version': PERIOD_CONTRACT_VERSION,
                    'view_version': view['view_version'], 'generator_version': view['generator_version'],
                    'seed': view['seed'], 'periods': view['periods'], 'objects': view['objects'],
                    'metrics': METRICS, 'demo_caliber': PVG.DEMO_CALIBER, 'notes': view['notes']}

        if op == 'states':
            pids = req.get('product_ids')
            if pids is None:
                # 未指定成员时使用可调查范围全池；存在根任务商品上界时取其内成员。
                pids = _bound_pool(view, allowed_product_ids)
            _require(isinstance(pids, list), 'invalid_structure', 'request.product_ids 必须是数组')
            has_base = 'base_period_id' in req
            has_target = 'target_period_id' in req
            _require(has_base == has_target, 'missing_state_window',
                     'states 显式周期窗口须同时给 base_period_id 和 target_period_id')
            base_period = _validate_period_id(req['base_period_id']) if has_base else None
            target_period = _validate_period_id(req['target_period_id']) if has_target else None
            out = [object_states(pid, view=view, base_period_id=base_period,
                                 target_period_id=target_period) for pid in pids]
            return {'event': 'period_object_states', 'contract_version': PERIOD_CONTRACT_VERSION,
                    'objects': out, 'demo_caliber': PVG.DEMO_CALIBER}

        if op == 'value':
            _require(isinstance(req.get('metrics'), list), 'invalid_structure',
                     'request.metrics 必须是数组')
            pids, scope_resolution = _resolve_query_scope(req, view, allowed_product_ids)
            has_window = 'window' in req
            has_period = 'period_id' in req
            if has_window:
                _require(not has_period, 'period_mode_conflict',
                         'value 的 period_id 与 window 不可混用（时间模式唯一确定）')
                return window_values(pids, req.get('window'),
                                     req.get('metrics'), view=view,
                                     aggregate=req.get('aggregate'),
                                     group_by=req.get('group_by'),
                                     scope_resolution=scope_resolution)
            _require(has_period, 'missing_period_id', 'value 须给 period_id 或 window 之一')
            return period_values(pids, _validate_period_id(req.get('period_id')),
                                 req.get('metrics'), view=view,
                                 aggregate=req.get('aggregate'),
                                 group_by=req.get('group_by'),
                                 scope_resolution=scope_resolution)

        if op == 'diff':
            # 范围与指标形态解析（scope/product_ids 互斥；metrics[]/metric 互斥）。
            pids, scope_resolution = _resolve_query_scope(req, view, allowed_product_ids)
            # 先检查时间形态冲突，再解析指标，使窗口错误优先返回。
            has_window_pair = 'base_window' in req or 'target_window' in req
            if has_window_pair:
                _require(not ('base_period_id' in req or 'target_period_id' in req),
                         'period_mode_conflict',
                         'diff 的周期键与日期窗口键不可混用（时间模式唯一确定）')
                _require('base_window' in req and 'target_window' in req, 'missing_window_keys',
                         'diff 日期窗口须同时给 base_window 与 target_window')
            metrics = _resolve_diff_metrics(req)
            if has_window_pair:
                if len(metrics) == 1:
                    return window_diff(pids, metrics[0], req.get('base_window'),
                                       req.get('target_window'), view=view,
                                       scope_resolution=scope_resolution)
                return window_diff_multi(pids, metrics, req.get('base_window'),
                                         req.get('target_window'), view=view,
                                         scope_resolution=scope_resolution)
            if len(metrics) == 1:
                # 单指标比较返回顶层 results 与 summary，不包装 by_metric。
                metric = metrics[0]
                diffs = [period_diff(pid, metric, req.get('base_period_id'),
                                     req.get('target_period_id'), view=view)
                         for pid in pids]
                _require(diffs, 'missing_objects', 'product_ids 须为非空数组')
                summary = _diff_set_summary(diffs)
                # 品类跨期合计仅在可加总指标的多商品比较中产生。
                totals = _category_totals(diffs, view, metric)
                if totals is not None:
                    summary['category_totals'] = totals
                out = {'event': 'period_diff_set', 'contract_version': PERIOD_CONTRACT_VERSION,
                       'metric': metric, **_metric_semantics(metric), 'results': diffs,
                       'summary': summary, 'demo_caliber': PVG.DEMO_CALIBER}
                if scope_resolution is not None:
                    out['scope_resolution'] = scope_resolution
                return out
            # 多指标：by_metric 信封；顶层不再出现 results / metric / summary。
            return period_diff_multi(pids, metrics, req.get('base_period_id'),
                                      req.get('target_period_id'), view=view,
                                      scope_resolution=scope_resolution)

        if op == DAILY_SERIES_OP:
            _require(isinstance(req.get('product_ids'), list), 'invalid_structure',
                     'request.product_ids 必须是数组')
            _require(isinstance(req.get('metrics'), list), 'invalid_structure',
                     'request.metrics 必须是数组')
            _require('window' in req, 'missing_window_keys',
                     'daily_series 须给日期窗口 window（周期选择器不决定日/月能力）')
            return daily_series(req.get('product_ids'), req.get('window'), req.get('metrics'),
                                view=view)

        if op == 'cohort_filter':
            # 时间模式键级互斥（值级校验在 period_cohort_filter 内做：键存在但值非法/缺失
            # 仍按对应码拒绝）；两套周期键混用、双期键缺一、全缺一律确定性拒绝。
            has_single = 'period_id' in req
            has_base = 'base_period_id' in req
            has_target = 'target_period_id' in req
            _require(not (has_single and (has_base or has_target)), 'period_mode_conflict',
                     'cohort_filter 单期 period_id 与双期 base/target_period_id 不可混用'
                     '（时间模式由周期键唯一确定）')
            _require(has_base == has_target, 'period_pair_incomplete',
                     'cohort_filter 双期条件须同时给 base_period_id 和 target_period_id')
            _require(has_single or (has_base and has_target), 'missing_period_id',
                     'cohort_filter 须给单期 period_id 或成对 base_period_id+target_period_id')
            return period_cohort_filter(req.get('scope'), req.get('conditions'),
                                        req.get('period_id'), req.get('base_period_id'),
                                        req.get('target_period_id'), view=view,
                                        allowed_product_ids=allowed_product_ids)

        pids, scope_resolution = _resolve_query_scope(req, view, allowed_product_ids)
        if 'window' in req:
            _require('period_id' not in req, 'period_mode_conflict',
                     'set_compare 的 period_id 与 window 不可混用（时间模式唯一确定）')
            return window_set_compare(pids, req.get('window'),
                                      req.get('metric') or _DEFAULT_SET_METRIC,
                                      req.get('kind') or 'median', view=view,
                                      scope_resolution=scope_resolution)
        return period_set_compare(pids, _validate_period_id(req.get('period_id')),
                                  req.get('metric') or _DEFAULT_SET_METRIC,
                                  req.get('kind') or 'median', view=view,
                                  scope_resolution=scope_resolution)
    except QueryRejected as e:
        return {'event': 'period_rejected', 'error_code': e.code, 'detail': e.detail,
                'request_echo': cq, 'demo_caliber': PVG.DEMO_CALIBER}


__all__ = ['PERIOD_CONTRACT_VERSION', 'METRICS', 'METRIC_KEYS', 'QUERY_OPS',
           'QUERY_REQUEST_KEYS', 'OP_REQUEST_KEYS', 'DIFF_STATES', 'validate_period_query_envelope',
           'build_view', 'metric_value', 'period_values', 'period_diff', 'period_set_compare',
           'period_cohort_filter', 'object_states', 'execute_period_query',
           'period_diff_multi', 'window_diff_multi',
           'COHORT_OP', 'COHORT_SCOPE_KINDS', 'COHORT_FIELD_SINGLE', 'COHORT_FIELDS_PAIR',
           'COHORT_OPERATORS', 'COHORT_MAX_CONDITIONS', 'COHORT_MAX_SCOPE_IDS',
           'COHORT_REQUEST_KEYS',
           'AGGREGATE_METRICS', 'AGGREGATE_MIN_PRODUCTS',
           'SCOPE_KINDS', 'SCOPE_KEYS', 'GROUP_BY_VALUES', 'resolve_scope',
           'DAILY_SERIES_OP', 'window_values', 'window_diff', 'window_set_compare', 'daily_series',
           'ST_OK', 'ST_ZERO_DENOM', 'ST_PERIOD_MISSING',
           'ST_FIELD_MISSING', 'ST_NOT_APPLICABLE', 'ST_WINDOW_INCOMPARABLE',
           'OBJ_IDENTITY_INCONSISTENT', 'OBJ_STATE_CHANGE', 'OBJ_ATTRIBUTE_DRIFT',
           'OBJ_RECORD_CONFLICT']
