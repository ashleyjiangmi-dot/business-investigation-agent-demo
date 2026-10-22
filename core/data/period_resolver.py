# -*- coding: utf-8 -*-
"""将结构化 time 参数解析为周期身份或真实日期窗口。

周期形态按允许周期中的最新日期回溯，时间序由日期与 period_index 决定，不按 ID 排序。
日期形态以模拟业务日为锚点，范围受日级覆盖约束，不受周周期选择限制。
拒绝混用时间写法、超范围窗口和错误的比较方向；解析结果随响应返回。
本模块只解析时间身份，取数与比较由查询层负责。"""
import calendar
import copy
import datetime as _dt
import os
import re
import sys

from . import period_view_generator as PVG   # noqa: E402
from . import operating_catalog as OC        # noqa: E402  （日级覆盖/模拟业务日单点）

PERIOD_TIME_SEMANTICS_VERSION = 'period-time-semantics-2.0'
PERIOD_RESOLVER_VERSION = 'period-resolver-2.0'

# 命名回溯档（与 offset 表达等价；N 期回溯用 offset -N，不扩命名档）
WHICH_OFFSETS = {'latest': 0, 'previous': -1, 'two_ago': -2}

# 形状与操作的对齐（与 qctx_period._OPS 对齐；catalog 无周期绑定，不接受 time）。
# cohort_filter 是一个 op 两种互斥时间模式——单期形状与窗口形状都合法（由 time
# 形状或显式周期键二选一决定），故同时进入两个元组；互斥性由既有形状检查（time 只允许
# 恰一种形状）与数据层 period_mode_conflict 拒绝保证，不新增第三种时间形状。
SINGLE_PERIOD_OPS = ('value', 'set_compare', 'cohort_filter')
WINDOW_OPS = ('diff', 'states', 'cohort_filter')

# 日期窗口形态的操作面（与周期形态分开表达——时间模型由槽位形态决定，不由 op 猜）。
SINGLE_WINDOW_OPS = ('value', 'set_compare', 'daily_series')
WINDOW_PAIR_OPS = ('diff',)

EXPLICIT_PERIOD_KEYS = ('period_id', 'base_period_id', 'target_period_id')
# 显式日期窗口请求键（不写 time 时模型可直接给真实日期）；与 time 语义不可混用。
EXPLICIT_WINDOW_KEYS = ('window', 'base_window', 'target_window')

# 槽位形态键（一个槽位恰给一种形态）
_PERIOD_FORM_KEYS = ('which', 'offset')
_DATE_FORM_KEYS = ('days_ago', 'weeks_ago', 'date', 'date_range', 'last_days',
                   'month', 'month_to_date')
_SLOT_KEYS = set(_PERIOD_FORM_KEYS) | set(_DATE_FORM_KEYS)

# 公共别名：模型可见 schema（core/prompts.py）按这两个元组派生槽位属性面与 oneOf；
# 日期形态的操作白名单由 SINGLE_WINDOW_OPS/WINDOW_PAIR_OPS 派生——键名与操作面单一事实源，
# 不在 Prompt 侧手抄第二份（改了这里即整面生效）。
PERIOD_FORM_KEYS = _PERIOD_FORM_KEYS
DATE_FORM_KEYS = _DATE_FORM_KEYS
DATE_FORM_OPS = tuple(dict.fromkeys(SINGLE_WINDOW_OPS + WINDOW_PAIR_OPS))

MODEL_PERIOD = 'period_identity'
MODEL_DATE = 'date_window'


def allowed_periods_in_time_order(allowed_periods):
    """本轮允许周期按目录真实时间序排列（日期区间为准；并列时按 period_index 兜底）。

    只保留合同周期集内的 id（未知 id 由 root_init 拒绝，这里防御性忽略）；纯函数零副作用。
    """
    known = {str(p['period_id']): p for p in PVG.PERIODS}
    rows = [dict(known[str(p)]) for p in (allowed_periods or []) if str(p or '') in known]
    rows.sort(key=lambda p: (p['period_start_date'], p['period_end_date'], p['period_index']))
    return rows


def _slot_meta(row, role, offset):
    """一个解析槽位的确定性元数据（canonical ID + 日期 + 目录时间序 + 相对锚点偏移）。"""
    return {'role': role, 'period_id': row['period_id'], 'period_label': row['period_label'],
            'period_start_date': row['period_start_date'], 'period_end_date': row['period_end_date'],
            'period_index': row['period_index'], 'offset': offset}


def _window_head(time_spec, op):
    return {'resolver_version': PERIOD_RESOLVER_VERSION,
            'semantics_version': PERIOD_TIME_SEMANTICS_VERSION,
            'source': 'time_semantics', 'op': str(op or ''),
            'semantics': copy.deepcopy(time_spec)}


def _fail(time_spec, op, error_code, detail, check_failed):
    """失败形态的 resolved_window：checks 里只置失败的轴，其余 not_applicable。"""
    window = _window_head(time_spec, op)
    window.update({'ok': False, 'error_code': error_code, 'detail': detail,
                   'checks': {'scope': ('failed' if check_failed == 'scope' else 'not_applicable'),
                              'direction': ('failed' if check_failed == 'direction'
                                            else 'not_applicable')}})
    return {'ok': False, 'error_code': error_code, 'detail': detail, 'window': window}


def _parse_slot(spec, role):
    """S（单期回溯表达）→ offset（≤0）整数；(None, 错误) 表示结构非法。纯解析，不做范围判断。"""
    if not isinstance(spec, dict):
        return None, ('time_semantics_invalid',
                      'time.%s 须为 JSON 对象（got %s）' % (role, type(spec).__name__))
    unknown = sorted(set(spec.keys()) - {'which', 'offset'})
    if unknown:
        return None, ('time_semantics_invalid',
                      'time.%s 含未知键: %s（只允许 which/offset 二选一）' % (role, ','.join(unknown)))
    has_which, has_offset = 'which' in spec, 'offset' in spec
    if has_which == has_offset:
        return None, ('time_semantics_invalid',
                      'time.%s 须恰好给 which 或 offset 之一' % role)
    if has_which:
        which = spec.get('which')
        if which not in WHICH_OFFSETS:
            return None, ('time_semantics_invalid',
                          'time.%s.which=%r 不在 %s' % (role, which, '/'.join(WHICH_OFFSETS)))
        return WHICH_OFFSETS[which], None
    offset = spec.get('offset')
    if isinstance(offset, bool) or not isinstance(offset, int):
        return None, ('time_semantics_invalid',
                      'time.%s.offset=%r 须为整数（≤0；0=最新期，-1=上一期，-N=回溯 N 期）'
                      % (role, offset))
    if offset > 0:
        return None, ('time_semantics_invalid',
                      'time.%s.offset=%d 须 ≤0（只表达向过去回溯；正向未来期不存在）' % (role, offset))
    return offset, None


# ===================== ：日期窗口形态解析（period-time-semantics-2.0） =====================

def _anchors():
    """(模拟业务日, 最新完整日, 覆盖起, 覆盖止)——全部为 date 对象（单点=operating_catalog）。"""
    return (_dt.date.fromisoformat(OC.DEMO_BUSINESS_DATE),
            _dt.date.fromisoformat(OC.LATEST_COMPLETE_DATE),
            _dt.date.fromisoformat(OC.DATA_COVERAGE_START),
            _dt.date.fromisoformat(OC.DATA_COVERAGE_END))


def _invalid(role, detail):
    return None, None, ('time_semantics_invalid', 'time.%s %s' % (role, detail))


def _parse_iso_date(value, role, field):
    try:
        return _dt.date.fromisoformat(str(value)), None
    except (TypeError, ValueError):
        return None, ('time_semantics_invalid',
                      'time.%s.%s=%r 不是合法 ISO 日期（YYYY-MM-DD）' % (role, field, value))


def _clip_to_coverage(start, end, role):
    _, _, cov_s, cov_e = _anchors()
    if start < cov_s or end > cov_e:
        return None, ('time_out_of_allowed_scope',
                      'time.%s 解析为 %s~%s，落出日级数据覆盖 %s~%s（日期窗口只按实际覆盖范围'
                      '解释，不看周/月选择器）' % (role, start.isoformat(), end.isoformat(),
                                                  cov_s.isoformat(), cov_e.isoformat()))
    return (start.isoformat(), end.isoformat()), None


def _month_window(selector, role, align):
    """`month` / `month_to_date` 选择器 → (start, end)；align=True 取月内同进度。

    - `this`=模拟业务日所在月；`last`=其上一个自然月；`YYYY-MM`=指定月。
    - `month`：完整自然月；当月未完结时取**覆盖到的部分**（如 5 月 → 05-01~05-27），
      不把未完结当月当完整月。
    - `month_to_date`：与**最新完整日同进度**对齐——同日/同月取该完整日，其余月份取到
      「同 day-of-month」（如 04-01~04-27、03-01~03-27）；不因月长不同而错位。
    """
    biz, latest, _, _ = _anchors()
    if selector == 'this':
        y, m = biz.year, biz.month
    elif selector == 'last':
        first = _dt.date(biz.year, biz.month, 1)
        prev = first - _dt.timedelta(days=1)
        y, m = prev.year, prev.month
    elif isinstance(selector, str) and re.match(r'^\d{4}-\d{2}$', selector):
        y, m = int(selector[:4]), int(selector[5:7])
        if not (1 <= m <= 12):
            return _invalid(role, '月份选择器 %r 非法（01–12）' % selector)
    else:
        return _invalid(role, '取值须为 this/last/YYYY-MM（got %r）' % (selector,))
    last_day = calendar.monthrange(y, m)[1]
    start = _dt.date(y, m, 1)
    if align:
        end = _dt.date(y, m, min(latest.day, last_day)) if (y, m) != (latest.year, latest.month) \
            else latest
    elif (y, m) == (biz.year, biz.month):
        end = min(_dt.date(y, m, last_day), _anchors()[3])     # 当月未完结 → 取覆盖到的部分
    else:
        end = _dt.date(y, m, last_day)
    return _clip_to_coverage(start, end, role)


def _parse_date_slot(spec, role):
    """日期形态槽位 → ((start_iso, end_iso), None) 或 (None, 错误)。纯解析+覆盖校验。"""
    form = [k for k in _DATE_FORM_KEYS if k in spec]
    if len(form) != 1:
        return None, ('time_semantics_invalid',
                      'time.%s 须恰好给一种日期形态 %s 之一' % (role, '/'.join(_DATE_FORM_KEYS)))
    key = form[0]
    biz, latest, _, cov_e = _anchors()
    if key in ('days_ago', 'weeks_ago', 'last_days'):
        value = spec.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None, ('time_semantics_invalid',
                          'time.%s.%s=%r 须为整数' % (role, key, value))
        minimum = 0 if key in ('days_ago', 'weeks_ago') else 1
        if value < minimum:
            return None, ('time_semantics_invalid',
                          'time.%s.%s=%d 须 ≥ %d' % (role, key, value, minimum))
        if key == 'days_ago':
            d = biz - _dt.timedelta(days=value)
            if d > cov_e:
                d = cov_e                                       # 回推不早于覆盖止（业务日无数据）
            return _clip_to_coverage(d, d, role)
        if key == 'weeks_ago':
            # 「上周同日」＝与最新完整日相隔整周（1=上周同日、2=两周前的同一星期几）。
            d = latest - _dt.timedelta(days=7 * value)
            return _clip_to_coverage(d, d, role)
        end = latest
        start = latest - _dt.timedelta(days=value - 1)
        return _clip_to_coverage(start, end, role)
    if key == 'date':
        d, err = _parse_iso_date(spec.get('date'), role, 'date')
        if err:
            return None, err
        return _clip_to_coverage(d, d, role)
    if key == 'date_range':
        raw = spec.get('date_range')
        if not isinstance(raw, dict):
            return None, ('time_semantics_invalid', 'time.%s.date_range 须为 JSON 对象' % role)
        extra = sorted(set(raw.keys()) - {'start_date', 'end_date'})
        if extra:
            return None, ('time_semantics_invalid',
                          'time.%s.date_range 含未知键: %s' % (role, ','.join(extra)))
        if 'start_date' not in raw or 'end_date' not in raw:
            return None, ('time_semantics_invalid',
                          'time.%s.date_range 须同时给 start_date 与 end_date' % role)
        s, err = _parse_iso_date(raw.get('start_date'), role, 'date_range.start_date')
        if err:
            return None, err
        e, err = _parse_iso_date(raw.get('end_date'), role, 'date_range.end_date')
        if err:
            return None, err
        if s > e:
            return None, ('time_semantics_invalid',
                          'time.%s.date_range 起点 %s 晚于终点 %s' % (role, s.isoformat(),
                                                                      e.isoformat()))
        return _clip_to_coverage(s, e, role)
    if key in ('month', 'month_to_date'):
        return _month_window(spec.get(key), role, align=(key == 'month_to_date'))
    return None, ('time_semantics_invalid', 'time.%s 日期形态 %r 未实现' % (role, key))


def _parse_slot_v2(spec, role):
    """槽位 → (kind, payload, 错误)。kind∈{'period','date'}；payload=offset 或 (start,end)。

    槽位形态由**实际给的键**唯一确定（周期形态 which/offset vs 日期形态 days_ago/date/…）；
    一个槽位混用两族或给多形态一律拒绝。
    """
    if not isinstance(spec, dict):
        return None, None, ('time_semantics_invalid',
                            'time.%s 须为 JSON 对象（got %s）' % (role, type(spec).__name__))
    unknown = sorted(set(spec.keys()) - _SLOT_KEYS)
    if unknown:
        return None, None, ('time_semantics_invalid',
                            'time.%s 含未知键: %s（周期形态 which/offset；日期形态 %s）'
                            % (role, ','.join(unknown), '/'.join(_DATE_FORM_KEYS)))
    period_forms = [k for k in _PERIOD_FORM_KEYS if k in spec]
    date_forms = [k for k in _DATE_FORM_KEYS if k in spec]
    if period_forms and date_forms:
        return None, None, ('time_semantics_invalid',
                            'time.%s 不可混用周期形态（%s）与日期形态（%s）'
                            % (role, '/'.join(period_forms), '/'.join(date_forms)))
    if date_forms:
        window, err = _parse_date_slot(spec, role)
        if err:
            return None, None, err
        return MODEL_DATE, window, None
    offset, err = _parse_slot(spec, role)
    if err:
        return None, None, err
    return MODEL_PERIOD, offset, None


def _resolve_date_window(time_spec, op, roles, windows, single_shape):
    """日期窗口形态的窗口 artifact（含真实起止日期、覆盖天数、方向/范围校验）。

    与周期形态一样，解析产物只是**确定性的身份/范围注入**（`window`/`base_window`/
    `target_window`）；具体取数/比较语义仍归 qctx_period。
    """
    allowed_ops = SINGLE_WINDOW_OPS if single_shape else WINDOW_PAIR_OPS
    if op not in allowed_ops:
        return _fail(time_spec, op, 'time_semantics_invalid',
                     'time 日期形态只适配 %s 操作（op=%r）；周期形态请用 which/offset'
                     % ('/'.join(allowed_ops), op or '（缺）'), 'scope')
    biz, latest, cov_s, cov_e = _anchors()
    slots = []
    for role in roles:
        s, e = windows[role]
        n_days = (_dt.date.fromisoformat(e) - _dt.date.fromisoformat(s)).days + 1
        slots.append({'role': role, 'start_date': s, 'end_date': e,
                      'granularity': PVG.DAILY_GRANULARITY, 'n_days': n_days})
    anchor = {'business_date': biz.isoformat(), 'latest_complete_date': latest.isoformat(),
              'coverage_start': cov_s.isoformat(), 'coverage_end': cov_e.isoformat()}
    window = _window_head(time_spec, op)
    if single_shape:
        s, e = windows['period']
        window.update({'ok': True, 'error_code': '', 'detail': '', 'mode': MODEL_DATE,
                       'anchor': anchor,
                       'resolved': {'window': {'start_date': s, 'end_date': e}},
                       'slots': slots,
                       'checks': {'scope': 'passed', 'direction': 'not_applicable'}})
        return {'ok': True, 'error_code': '', 'detail': '', 'window': window}
    bs, be = windows['base']
    ts, te = windows['target']
    if bs >= ts:
        return _fail(time_spec, op, 'time_window_direction_invalid',
                     '日期窗口方向不成立：base=%s~%s 不早于 target=%s~%s。base 须早于 target'
                     '（按真实起止日期判断，不按文字顺序）' % (bs, be, ts, te), 'direction')
    window.update({'ok': True, 'error_code': '', 'detail': '', 'mode': MODEL_DATE,
                   'anchor': anchor,
                   'resolved': {'base_window': {'start_date': bs, 'end_date': be},
                                'target_window': {'start_date': ts, 'end_date': te}},
                   'slots': slots,
                   'checks': {'scope': 'passed', 'direction': 'passed'}})
    return {'ok': True, 'error_code': '', 'detail': '', 'window': window}


def resolve_time_semantics(time_spec, allowed_periods, op):
    """模型 time 语义 → canonical 窗口（纯函数、确定性）。

    返回 {'ok': bool, 'error_code': str, 'detail': str, 'window': {...}}；ok=False 时 window
    仍带回原始语义与失败原因（resolved_window 的失败形态，供响应回显与根任务登记）。
    """
    op = str(op or '')
    if not isinstance(time_spec, dict):
        return _fail(time_spec, op, 'time_semantics_invalid',
                     'time 须为 JSON 对象（got %s）' % type(time_spec).__name__, 'scope')
    unknown = sorted(set(time_spec.keys()) - {'period', 'base', 'target'})
    if unknown:
        return _fail(time_spec, op, 'time_semantics_invalid',
                     'time 含未知键: %s（单期用 {"period":S}，窗口用 {"base":S,"target":S}）'
                     % ','.join(unknown), 'scope')
    has_period = 'period' in time_spec
    has_base = 'base' in time_spec
    has_target = 'target' in time_spec
    if has_period == (has_base or has_target):
        return _fail(time_spec, op, 'time_semantics_invalid',
                     'time 须恰为一种形状：单期 {"period":S} 或窗口 {"base":S,"target":S}'
                     '（base/target 须同时给）', 'scope')
    if has_period:
        roles = ('period',)
    else:
        if not (has_base and has_target):
            return _fail(time_spec, op, 'time_semantics_invalid',
                         'time 窗口形状须同时给 base 与 target', 'scope')
        roles = ('base', 'target')
    offsets = {}
    kinds = {}
    for role in roles:
        kind, payload, err = _parse_slot_v2(time_spec[role], role)
        if err:
            return _fail(time_spec, op, err[0], err[1], 'scope')
        kinds[role] = kind
        offsets[role] = payload
    if len(set(kinds.values())) > 1:
        return _fail(time_spec, op, 'time_semantics_invalid',
                     '同一 time 不可混用周期形态与日期形态：%s'
                     % '，'.join('%s=%s' % (r, kinds[r]) for r in roles), 'scope')
    if set(kinds.values()) == {MODEL_DATE}:
        return _resolve_date_window(time_spec, op, roles, offsets, has_period)
    if has_period:
        if op not in SINGLE_PERIOD_OPS:
            return _fail(time_spec, op, 'time_semantics_invalid',
                         'time 单期形状只适配 %s 操作（op=%r）；窗口比较请用 base/target 形状'
                         % ('/'.join(SINGLE_PERIOD_OPS), op or '（缺）'), 'scope')
    elif op not in WINDOW_OPS:
        return _fail(time_spec, op, 'time_semantics_invalid',
                     'time 窗口形状只适配 %s 操作（op=%r）；单期取值请用 period 形状'
                     % ('/'.join(WINDOW_OPS), op or '（缺）'), 'scope')
    order = allowed_periods_in_time_order(allowed_periods)
    if not order:
        return _fail(time_spec, op, 'time_out_of_allowed_scope',
                     '本轮无允许周期：相对时间无法解析（周期由入口选择带入）', 'scope')
    anchor = order[-1]
    deepest_back = len(order) - 1
    resolved_rows, slots = {}, []
    for role in roles:
        idx = len(order) - 1 + offsets[role]
        if idx < 0:
            return _fail(time_spec, op, 'time_out_of_allowed_scope',
                         'time.%s（%s=回溯 %d 期）落出本轮允许周期：允许范围内最多回溯 %d 期'
                         '（锚=允许周期中目录日期最晚的 %s）；需要更早期时由入口扩允许范围，'
                         '不从题面扩展' % (role, role, -offsets[role], deepest_back,
                                          anchor['period_id']), 'scope')
        row = order[idx]
        resolved_rows[role] = row
        slots.append(_slot_meta(row, role, offsets[role]))
    window = _window_head(time_spec, op)
    if len(roles) == 1:
        row = resolved_rows['period']
        window.update({'ok': True, 'error_code': '', 'detail': '',
                       'anchor': {k: anchor[k] for k in ('period_id', 'period_label',
                                                         'period_start_date', 'period_end_date',
                                                         'period_index')},
                       'resolved': {'period_id': row['period_id']},
                       'slots': slots,
                       'checks': {'scope': 'passed', 'direction': 'not_applicable'}})
    else:
        base_row, target_row = resolved_rows['base'], resolved_rows['target']
        if base_row['period_index'] >= target_row['period_index']:
            return _fail(time_spec, op, 'time_window_direction_invalid',
                         '窗口方向不成立：base=%s（目录时间序第 %d 期）不早于 target=%s（第 %d 期）。'
                         'base 须早于 target（时间序以目录日期区间/period_index 为准，不以 ID '
                         '字符串判断）' % (base_row['period_id'], base_row['period_index'],
                                          target_row['period_id'], target_row['period_index']),
                         'direction')
        window.update({'ok': True, 'error_code': '', 'detail': '',
                       'anchor': {k: anchor[k] for k in ('period_id', 'period_label',
                                                         'period_start_date', 'period_end_date',
                                                         'period_index')},
                       'resolved': {'base_period_id': base_row['period_id'],
                                    'target_period_id': target_row['period_id']},
                       'slots': slots,
                       'checks': {'scope': 'passed', 'direction': 'passed'}})
    return {'ok': True, 'error_code': '', 'detail': '', 'window': window}


def resolve_request(req, allowed_periods):
    """解析请求中的 time；没有 time 时交由显式周期或日期窗口处理。

    time 与显式 period_id、比较周期或 window 字段不能同时出现，混用时返回结构化拒绝。"""
    if not isinstance(req, dict) or 'time' not in req:
        return None
    time_spec = req.get('time')
    op = str(req.get('op') or '')
    mixed = sorted((set(EXPLICIT_PERIOD_KEYS) | set(EXPLICIT_WINDOW_KEYS)) & set(req.keys()))
    if mixed:
        return _fail(time_spec, op, 'time_semantics_invalid',
                     'time 与显式周期/日期键 %s 不可混用：相对时间用 time 字段，canonical 请求'
                     '直接给周期 ID 或真实日期窗口，二选一' % ','.join(mixed), 'scope')
    return resolve_time_semantics(time_spec, allowed_periods, op)


def apply_resolution(req, window):
    """把解析出的身份/窗口注入请求副本：剥离 `time` 键、注入 period_id/base/target 或
    window/base_window/target_window（日期形态）。

    返回新 dict（不修改入参）。注入后的请求与显式（周期或日期）请求同形——交回现有
    oagent_query/pctx 合同执行（底层数据查询只吃 canonical 周期 ID 或真实日期窗口，
    缓存指纹对"同窗口的语义/显式两种写法"自然归一）。
    """
    out = {k: v for k, v in req.items() if k != 'time'}
    out.update(window['resolved'])
    return out


__all__ = ['PERIOD_TIME_SEMANTICS_VERSION', 'PERIOD_RESOLVER_VERSION', 'WHICH_OFFSETS',
           'SINGLE_PERIOD_OPS', 'WINDOW_OPS', 'EXPLICIT_PERIOD_KEYS', 'EXPLICIT_WINDOW_KEYS',
           'SINGLE_WINDOW_OPS', 'WINDOW_PAIR_OPS', 'MODEL_PERIOD', 'MODEL_DATE',
           'PERIOD_FORM_KEYS', 'DATE_FORM_KEYS', 'DATE_FORM_OPS',
           'allowed_periods_in_time_order', 'resolve_time_semantics', 'resolve_request',
           'apply_resolution']
