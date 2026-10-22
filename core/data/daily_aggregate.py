# -*- coding: utf-8 -*-
"""按日期范围聚合模拟日级事实。

订单量与播放量累加；点击率取所有有效行的条均，不平均每日比率。
OPM 为累计订单/累计播放 ×1000，GPM 为累计成交额分子/累计播放。
任一天事实缺失使整个窗口返回 period_missing；任一冲突日使窗口返回 record_conflict。
零分母不回退，缺失不代零。"""
from datetime import date as _date

from . import period_view_generator as PVG
from . import operating_catalog as OC
from . import qctx_period as QP
from . import video_eligibility as VE
from .query_contract import QueryRejected, require as _require

DAILY_CONTRACT_VERSION = 'pctx_daily_v1'

COVERAGE_START = OC.DATA_COVERAGE_START
COVERAGE_END = OC.DATA_COVERAGE_END
DEMO_BUSINESS_DATE = OC.DEMO_BUSINESS_DATE
LATEST_COMPLETE_DATE = OC.LATEST_COMPLETE_DATE

_daily_view_cache = {}
_daily_index_cache = {}


def daily_view(seed=PVG.DEFAULT_SEED):
    """取日级事实施视图（按 seed memoize；纯内存）。"""
    if seed not in _daily_view_cache:
        _daily_view_cache[seed] = PVG.build_daily_view(seed)
    return _daily_view_cache[seed]


def daily_index(seed=PVG.DEFAULT_SEED):
    """({(product_id, date): [日事实...]})；通常 1 条，X4 冲突周为 2 条（variant 0/1）。"""
    if seed not in _daily_index_cache:
        idx = {}
        for day in daily_view(seed)['days']:
            idx.setdefault((day['product_id'], day['date']), []).append(day)
        _daily_index_cache[seed] = idx
    return _daily_index_cache[seed]


# ===================== 日期范围与月窗口 =====================

def coverage_dates():
    """覆盖区间内全部日期（升序 ISO 字符串）。"""
    return PVG.date_range(COVERAGE_START, COVERAGE_END)


def _as_date(d):
    try:
        return _date.fromisoformat(str(d))
    except ValueError:
        raise QueryRejected('invalid_date', 'date=%r 不是合法 ISO 日期（YYYY-MM-DD）' % (d,))


def _validated_range(start_date, end_date):
    s, e = _as_date(start_date), _as_date(end_date)
    _require(s <= e, 'invalid_range', 'start_date=%s 不早于/等于 end_date=%s'
             % (s.isoformat(), e.isoformat()))
    cs, ce = _date.fromisoformat(COVERAGE_START), _date.fromisoformat(COVERAGE_END)
    _require(cs <= s and e <= ce, 'out_of_coverage',
             '日期范围 %s~%s 超出日级数据覆盖 %s~%s'
             % (s.isoformat(), e.isoformat(), COVERAGE_START, COVERAGE_END))
    return PVG.date_range(s.isoformat(), e.isoformat())


def month_full_range(year, month):
    """仅返回整月都在数据覆盖内的自然月范围；未覆盖完整的月份明确拒绝，不当作完整月比较。"""
    import calendar
    y, m = int(year), int(month)
    last = calendar.monthrange(y, m)[1]
    start, end = '%04d-%02d-01' % (y, m), '%04d-%02d-%02d' % (y, m, last)
    cs, ce = _date.fromisoformat(COVERAGE_START), _date.fromisoformat(COVERAGE_END)
    _require(cs <= _date.fromisoformat(start) and _date.fromisoformat(end) <= ce,
             'incomplete_month',
             '%04d-%02d 未完整落在日级覆盖 %s~%s（末年只到 %s）：完整月请改用覆盖完整的月份，'
             '未完结月份用 month_to_date_range 取月内同进度范围'
             % (y, m, COVERAGE_START, COVERAGE_END, COVERAGE_END))
    return start, end


def month_to_date_range(year, month, through_date):
    """月内同进度范围 [月初, through_date]（“本月截至某日”对齐口径）。"""
    y, m = int(year), int(month)
    first = '%04d-%02d-01' % (y, m)
    end = _as_date(through_date).isoformat()
    _require(end[:7] == '%04d-%02d' % (y, m), 'invalid_range',
             'through_date=%s 不在 %04d-%02d 月内' % (end, y, m))
    _validated_range(first, end)          # 校验在覆盖区内
    return first, end


# ===================== 逐日分子/分母（口径与 metric_value 逐项对齐） =====================

def _day_components(day, metric):
    """单日分子/分母（**未四舍五入**），口径与 `qctx_period.metric_value` 逐项对齐。"""
    rows = day.get('video_rows') or []
    if metric == 'M1':
        return {'numerator': day.get('period_orders'), 'denominator': None,
                'kind': 'sum_no_denominator', 'n_rows': len(rows)}
    if metric == 'M2':
        vals = [r.get('vv') for r in rows if r.get('vv') is not None]
        return {'numerator': sum(vals) if vals else None, 'denominator': None,
                'kind': 'sum_no_denominator', 'n_rows': len(rows), 'contributing_rows': len(vals)}
    if metric == 'M3':
        vals = [r.get('plc_ctr') for r in rows if r.get('plc_ctr') is not None]
        return {'numerator': sum(vals) if vals else None, 'denominator': len(vals),
                'kind': 'row_count', 'n_rows': len(rows), 'contributing_rows': len(vals)}
    elig = [r for r in rows if VE.video_eligibility(r)[0]]
    if metric == 'M4':
        o = v = 0
        paired = 0
        for r in elig:
            a, b = r.get('paid_orders'), r.get('vv')
            if a is not None and b is not None:
                o += a
                v += b
                paired += 1
        return {'numerator': o if paired else None, 'denominator': v if paired else None,
                'kind': 'sum_vv', 'n_rows': len(rows), 'eligible_rows': len(elig),
                'ineligible_rows': len(rows) - len(elig), 'paired_rows': paired}
    num = den = 0.0
    g_with = 0
    paired = 0
    for r in elig:
        g, v = r.get('gpm'), r.get('vv')
        if g is None:
            continue
        g_with += 1
        if v is not None:
            num += g * v
            den += v
            paired += 1
    return {'numerator': num if paired else None, 'denominator': den if paired else None,
            'kind': 'sum_vv', 'n_rows': len(rows), 'eligible_rows': len(elig),
            'ineligible_rows': len(rows) - len(elig), 'gpm_rows': g_with, 'paired_rows': paired}


def _merge(components):
    """分别累加各日分子、分母及覆盖计数，供范围指标统一计算；不平均每日比率。"""
    num_present = [c['numerator'] for c in components if c['numerator'] is not None]
    den_present = [c['denominator'] for c in components if c['denominator'] is not None]
    return {
        'numerator': sum(num_present) if num_present else None,
        'denominator': sum(den_present) if den_present else None,
        'n_rows': sum(c.get('n_rows', 0) for c in components),
        'contributing_rows': sum(c.get('contributing_rows', 0) for c in components),
        'eligible_rows': sum(c.get('eligible_rows', 0) for c in components),
        'ineligible_rows': sum(c.get('ineligible_rows', 0) for c in components),
        'paired_rows': sum(c.get('paired_rows', 0) for c in components),
        'gpm_rows': sum(c.get('gpm_rows', 0) for c in components),
        'days_with_numerator': len(num_present),
        'days_with_denominator': len(den_present),
    }


def _block(metric, product_id, start_date, end_date, n_days, agg):
    """把合并后的分子/分母 → 值块；零分母/缺失规则与 qctx_period 一致（不静默代 0）。"""
    block = {'metric': metric, 'metric_name': QP.METRICS[metric]['name'],
             'unit': QP.METRICS[metric]['unit'], **QP._metric_semantics(metric),
             'product_id': product_id, 'granularity': PVG.DAILY_GRANULARITY,
             'period_start_date': start_date, 'period_end_date': end_date,
             'period_length_days': n_days, 'n_rows': agg['n_rows'],
             'demo_caliber': PVG.DEMO_CALIBER}
    num, den = agg['numerator'], agg['denominator']

    if metric == 'M1':
        block['denominator'] = {'kind': 'sum_no_denominator', 'value': None,
                                'note': '范围合计（逐日订单量相加）；无分母'}
        if num is None:
            return QP._unknown(block, 'field_missing', QP.ST_FIELD_MISSING)
        block.update(value=num, state=QP.ST_OK)
        return block

    if metric == 'M2':
        block['denominator'] = {'kind': 'sum_no_denominator', 'value': None,
                                'note': 'sum(vv)：无分母；参与行数=范围内非缺失 vv 行数'}
        block['contributing_rows'] = agg['contributing_rows']
        if agg['n_rows'] == 0:
            return QP._unknown(block, 'no_rows', QP.ST_FIELD_MISSING)
        if num is None:
            return QP._unknown(block, 'field_all_missing', QP.ST_FIELD_MISSING)
        block.update(value=QP._round_num(num), state=QP.ST_OK)
        return block

    if metric == 'M3':
        block['denominator'] = {'kind': 'row_count', 'value': den or 0,
                                'note': '条均口径：分母=范围内非缺失 plc_ctr 行数（非曝光加权）'}
        block['contributing_rows'] = agg['contributing_rows']
        if not den:
            return QP._unknown(block, 'zero_denominator', QP.ST_ZERO_DENOM)
        block.update(value=QP._round_num(num / den), state=QP.ST_OK)
        return block

    block['eligible_rows'] = agg['eligible_rows']
    block['ineligible_rows'] = agg['ineligible_rows']
    block['denominator'] = {'kind': 'sum_vv', 'value': QP._round_num(den or 0),
                            'note': ('Σvv（eligible 且 vv、paid_orders 配对行）' if metric == 'M4'
                                     else 'Σvv（eligible 且 gpm、vv 配对行；VV 加权分母）')}
    if metric == 'M4':
        block['paired_rows'] = agg['paired_rows']
        if not den or not agg['paired_rows']:
            return QP._unknown(block, 'zero_denominator', QP.ST_ZERO_DENOM)
        block.update(value=QP._round_num(num / den * 1000.0),
                     numerator_orders=QP._round_num(num), state=QP.ST_OK)
        return block

    #
    block['eligible_gpm_rows'] = agg['gpm_rows']
    block['gpm_agg'] = None
    if den and agg['paired_rows']:
        block.update(value=QP._round_num(num / den), gpm_agg='vv_weighted', state=QP.ST_OK)
        return block
    if agg['gpm_rows'] > 0:
        block['gpm_agg'] = 'period_zero_denominator_no_fallback'
        block['zero_denominator_note'] = PVG.GPM_ZERO_DENOMINATOR_NOTE
        return QP._unknown(block, 'zero_denominator', QP.ST_ZERO_DENOM)
    return QP._unknown(block, 'field_all_missing', QP.ST_FIELD_MISSING)


# ===================== 观测选择（缺失 / 冲突） =====================

def _variant_fingerprint(day):
    import json
    return json.dumps({'o': day.get('period_orders'), 'r': day.get('video_rows')},
                      ensure_ascii=False, sort_keys=True)


def _day_conflict(facts):
    """该日期是否为 X4 冲突日。

    冲突是**周记录身份**属性：`build_daily_view` 把冲突周派生的 7 天全部标记
    `record_conflict`；即使某天两条变体拆分后日值恰好相同，也按标记判冲突，不靠日级内容
    指纹“看起来一样”就丢语义。标记缺失时（防御）仍回落到指纹比对。
    """
    if not facts:
        return False
    if any(f.get('record_conflict') for f in facts):
        return True
    return len({_variant_fingerprint(f) for f in facts}) > 1


def _observations(product_id, dates, index):
    """返回 (observations, missing_dates, conflict_dates)。

    - 某天无日事实 → missing（X1 缺周期）；
    - 某天为 X4 冲突日（`_day_conflict`）→ conflict（不择一；与 `period_values` 同判）。
    """
    observations, missing, conflict = [], [], []
    for dt in dates:
        facts = index.get((product_id, dt)) or []
        if not facts:
            missing.append(dt)
            continue
        if _day_conflict(facts):
            conflict.append(dt)
            continue
        observations.append(facts[0])
    return observations, missing, conflict


def _unknown_range_block(metric, product_id, start_date, end_date, n_days, detail, state, extra=None):
    block = {'metric': metric, 'metric_name': QP.METRICS[metric]['name'],
             'unit': QP.METRICS[metric]['unit'], **QP._metric_semantics(metric),
             'product_id': product_id, 'granularity': PVG.DAILY_GRANULARITY,
             'period_start_date': start_date, 'period_end_date': end_date,
             'period_length_days': n_days, 'demo_caliber': PVG.DEMO_CALIBER}
    if extra:
        block.update(extra)
    return QP._unknown(block, detail, state)


# ===================== 范围读数 =====================

def aggregate_range(product_id, start_date, end_date, metrics, seed=PVG.DEFAULT_SEED,
                    _dates=None):
    """按窗口内各日累加分子和分母，再计算指标。

    缺日返回 unknown(period_missing)，冲突日返回 unknown(record_conflict)，均不补零或择一。"""
    dates = _dates if _dates is not None else _validated_range(start_date, end_date)
    pid = str(product_id)
    metrics = [QP._validate_metric(m) for m in metrics]
    _require(metrics, 'missing_metrics', 'metrics 须为非空数组')
    known = {str(o['product_id']) for o in PVG.PRODUCTS}
    _require(pid in known, 'unknown_object', '对象 %r 不在本视图目录' % pid)

    index = daily_index(seed)
    obs, missing, conflict = _observations(pid, dates, index)
    out = {'event': 'daily_range_values', 'contract_version': DAILY_CONTRACT_VERSION,
           'product_id': pid, 'start_date': start_date, 'end_date': end_date,
           'n_days': len(dates), 'dates': dates, 'metrics_requested': metrics,
           'missing_dates': missing, 'conflict_dates': conflict,
           'granularity': PVG.DAILY_GRANULARITY, 'demo_caliber': PVG.DEMO_CALIBER}

    if conflict:
        out['object_state'] = QP.OBJ_RECORD_CONFLICT
        out['results'] = [_unknown_range_block(m, pid, start_date, end_date, len(dates),
                                               'record_conflict', QP.OBJ_RECORD_CONFLICT)
                          for m in metrics]
        return out
    if missing:
        out['object_state'] = QP.ST_PERIOD_MISSING
        out['results'] = [_unknown_range_block(m, pid, start_date, end_date, len(dates),
                                               'period_missing', QP.ST_PERIOD_MISSING,
                                               {'missing_dates': missing}) for m in metrics]
        return out

    out['object_state'] = QP.ST_OK
    out['results'] = [_block(m, pid, start_date, end_date, len(dates),
                             _merge([_day_components(day, m) for day in obs]))
                      for m in metrics]
    return out


def daily_values(product_id, date_str, metrics, seed=PVG.DEFAULT_SEED):
    """单日读数（= `aggregate_range` 的 1 天特例）。"""
    return aggregate_range(product_id, date_str, date_str, metrics, seed=seed,
                           _dates=_validated_range(date_str, date_str))


def daily_components(product_id, date_str, metrics, seed=PVG.DEFAULT_SEED):
    """按日披露分子/分母（供“按日读再汇总”正确加权，而非平均每日比率）。

    与 `aggregate_range` 用**同一冲突判定**：X4 冲突日不返回任何变体的分子/分母
    （`record_conflict=True`、`metrics` 全 None），不静默取第一条“看起来正常”的记录。
    """
    _validated_range(date_str, date_str)
    pid = str(product_id)
    facts = daily_index(seed).get((pid, date_str)) or []
    conflict = _day_conflict(facts)
    return {'date': date_str, 'product_id': pid,
            'metrics_requested': [QP._validate_metric(m) for m in metrics],
            'present': bool(facts), 'record_conflict': conflict,
            'metrics': ({} if conflict else
                        {m: (_day_components(facts[0], m) if facts else None) for m in metrics}),
            'demo_caliber': PVG.DEMO_CALIBER}


__all__ = ['DAILY_CONTRACT_VERSION', 'COVERAGE_START', 'COVERAGE_END', 'DEMO_BUSINESS_DATE',
           'LATEST_COMPLETE_DATE', 'daily_view', 'daily_index', 'coverage_dates',
           'month_full_range', 'month_to_date_range', 'daily_values', 'aggregate_range',
           'daily_components']
