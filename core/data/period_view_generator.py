# -*- coding: utf-8 -*-
"""生成 60 商品 × 8 周期的模拟经营视图。纯函数，无文件 IO 或模型依赖。

商品身份与生成规则来自 operating_catalog；不读取快照、评测索引或题目答案。
GENERATOR_VERSION 与 seed 控制独立随机流，VIEW_VERSION 标识输出数据合同。
P01–P10 的 t6/t7 使用 SIM_PARAMS，其余单元使用档位、轨迹模板与局部随机流。
MAGNITUDE_SCALE 只缩放绝对量，不改变比率或随机流。

指标粒度为商品 × 周期：为订单合计；为播放合计；为非缺失点击率条均；
为 eligible 配对行的订单/播放 ×1000；为 eligible 配对行的播放加权成交额效率。
零分母不回退，返回 unknown；冲突身份整体排除并披露。

published 表示该期是否新增发布，不表示上下架；days_after_publish 为相对观察年龄。
全部周期值、发布状态与质量标记均为模拟数据，不作为真实日期、线上效果或因果证据。"""
import copy
import random

from . import operating_catalog as OC

VIEW_VERSION = 'opview-1.3'
GENERATOR_VERSION = 'ovgen-1.0'
DEFAULT_SEED = 20260527

# 绝对量统一缩放，但不改变随机流种子或比率指标。
MAGNITUDE_SCALE = 100

# 判定锚（与 对齐；仅作说明，本视图不把 row.days_after_publish 解释成绝对日期）
JUDGE_DATE = '2026-05-27'

DEMO_CALIBER = '模拟数据（Demo 口径），非真实平台日期、非线上效果、非生产数据'

# 周期元组前两项固定为默认基期和末期；真实时间序由 period_index 表达。
PERIODS = tuple(
    dict(p, demo_caliber=DEMO_CALIBER) for p in OC.PERIODS
)
PERIOD_IDS = tuple(p['period_id'] for p in PERIODS)
PERIOD_INDEX = {p['period_id']: p['period_index'] for p in PERIODS}
DEFAULT_PERIOD_ID = 'period_demo_p2'      # 默认经营周期。
# 差值默认基期/末期命名常量：与 PERIOD_IDS[0]/[1] 恒等（双保险），
# 8 周期扩容后不显式传周期的差值仍=p1→p2，不静默漂移。
DEFAULT_BASE_PERIOD_ID = 'period_demo_p1'
DEFAULT_TARGET_PERIOD_ID = 'period_demo_p2'

# 商品身份表来自 operating_catalog。
PRODUCTS = OC.PRODUCTS
PRODUCT_IDS = OC.PRODUCT_IDS
LEGACY_PRODUCT_IDS = OC.LEGACY_PRODUCT_IDS
# 缺陷载体表：经本模块转发，供查询/验收按 PVG 单一入口引用（数据本体在 operating_catalog）
X4_RECORD_CONFLICT = OC.X4_RECORD_CONFLICT
X5_IDENTITY_DRIFT = OC.X5_IDENTITY_DRIFT

# ---- Demo 设计参数（既有 10 商品 t6/t7 沿用；新增单元按档位/模板取参，见 OC.MAGNITUDE_TIERS）----
SIM_PARAMS = {
    'video_rows_per_product_period': 4,   # 每个商品每个周期的视频行数（既有商品）
    'vv_range': [300, 2500],              # 逐行播放量（既有商品 MID-既有档；覆盖 VV_MED≈983 两侧）
    'plc_ctr_range': [0.012, 0.045],      # 逐行 PLC 点击率（覆盖 PLC_CTR_MED≈0.0297 两侧）
    'cvr': 0.12,                          # 点击→订单转化率常数：与 sim_publish/simulation_generator 同值
    'public_p': 0.9,
    'poi_p': 0.85,
    'commercial_p': 0.9,
    'violation_p': 0.05,
    'breakout_p': 0.3333,                 # 出坡概率：既有基准 BREAKOUT_RATE_MED≈1/3 附近
    'days_after_publish_range': [1, 7],   # 周期内相对天数：行落在该 7 天周期内（不补造起止日）
    'recent_2d_same_poi_posts_range': [0, 3],
    'same_product_cold_start_videos_range': [0, 2],
    'off_group_order_ratio_range': [0.5, 1.5],   # =视频组订单合计+额外比例（保证 ≥ 视频组，区分口径）
}

PARAM_REASONS = {
    'video_rows_per_product_period': 'Demo 设计参数：足以构成可聚合行人口并保持题面可答；非行业分布',
    'vv_range': '既有 10 商品沿用（MID-既有档）；新增商品按 operating_catalog.MAGNITUDE_TIERS 三档',
    'plc_ctr_range': '既有 10 商品沿用；G7/G8 载体按 operating_catalog.G7/G8_CTR_RANGE 两轴同设',
    'cvr': '与 sim_publish/simulation_generator 同值（orders=播放×点击率×cvr），两层口径统一维护',
    'breakout_p': '既有基准出坡率中位数附近；出坡标记仅在本视图行上作演示字段，不参与 M1–M5 口径',
    'days_after_publish_range': '周期内相对天数（该行为落在本 7 天周期内的相对天数）；不补造绝对起止日',
    'off_group_order_ratio_range': 'M1 商品口径=视频组订单合计+额外比例（Demo 假设），保证 M1≥视频组、'
                                   '两个统计口径不互相冒充',
}

# 视频行字段独立声明，运行时不依赖评测模块。
VIDEO_ROW_KEYS = (
    'video_id', 'days_after_publish', 'is_public', 'has_valid_poi', 'has_commercial_tag',
    'has_violation_risk', 'in_cold_start', 'breakout', 'vv', 'gpm', 'opm', 'paid_orders',
    'plc_ctr', 'recent_2d_same_poi_posts', 'same_product_cold_start_videos',
)

COLD_START_MAX_DAY = 14
GPM_ZERO_DENOMINATOR_NOTE = ('M5 零分母规则=不回退：Σvv=0 → unknown(zero_denominator)，'
                             '专属标记 period_zero_denominator_no_fallback；'
                             '不继承旧实现 simple_mean_fallback(ΣVV=0)、不与旧值混同')


def period_by_id(period_id):
    for p in PERIODS:
        if p['period_id'] == str(period_id):
            return dict(p)
    return None


def product_by_id(product_id):
    for p in PRODUCTS:
        if p['product_id'] == str(product_id):
            return dict(p)
    return None


def _unit_seed_key(product_id, period_id, seed, suffix=''):
    """随机流种子串：(版本, 商品, 周期, 种子) 局部化；同输入→逐字节可复现。"""
    return '%s|%s|%s|%d%s' % (GENERATOR_VERSION, product_id, period_id, seed, suffix)


def _shape_seed_key(product_id, seed):
    """商品级形状流种子串：只服务「跨期稳定的水平量」抽样，与逐单元随机流互不干扰。"""
    return '%s|%s|%d|shape' % (GENERATOR_VERSION, product_id, seed)


def _product_shape_draws(product_id, seed):
    """为每个商品抽取跨期稳定的点击率位置和视频组外订单比例。

    固定商品级参数可保留轨迹模板的走势，避免逐期独立抽样的噪声掩盖趋势；行内仍保留微抖动。"""
    rng = random.Random(_shape_seed_key(product_id, seed))
    u_ctr = rng.random()
    lo, hi = SIM_PARAMS['off_group_order_ratio_range']
    return u_ctr, rng.uniform(lo, hi)


def _gen_rows(rng, n, vv_lo, vv_hi, ctr_lo, ctr_hi, cvr, published, age_range,
              order_value, period_id, product_id, ctr_base=None):
    """按参数和注入的随机流生成视频行。

    提供 ctr_base 时使用商品级稳定点击率并叠加微抖动；未提供时在区间内独立抽样。
    两条路径均消耗一次点击率随机抽样，保持后续随机流位置稳定。"""
    r2_lo, r2_hi = SIM_PARAMS['recent_2d_same_poi_posts_range']
    sc_lo, sc_hi = SIM_PARAMS['same_product_cold_start_videos_range']
    rows = []
    for i in range(n):
        days = rng.randint(*age_range)
        vv = rng.randint(vv_lo, vv_hi)
        u = rng.random()
        ctr = round(ctr_lo + (ctr_hi - ctr_lo) * u if ctr_base is None
                    else min(ctr_hi, max(ctr_lo, ctr_base * (0.9 + 0.2 * u))), 4)
        orders = int(vv * ctr * cvr)
        gpm = round(orders * order_value / vv * 1000.0, 4)
        opm = round(orders / vv * 1000.0, 4)
        rows.append({
            'video_id': '%s-%s-%03d' % (period_id.replace('period_demo_', ''),
                                        product_id[-4:], i + 1),
            'days_after_publish': days,
            'is_public': rng.random() < SIM_PARAMS['public_p'],
            'has_valid_poi': rng.random() < SIM_PARAMS['poi_p'],
            'has_commercial_tag': rng.random() < SIM_PARAMS['commercial_p'],
            'has_violation_risk': rng.random() < SIM_PARAMS['violation_p'],
            'in_cold_start': days <= COLD_START_MAX_DAY,
            'breakout': rng.random() < SIM_PARAMS['breakout_p'],
            'vv': vv, 'gpm': gpm, 'opm': opm, 'paid_orders': orders, 'plc_ctr': ctr,
            'recent_2d_same_poi_posts': rng.randint(r2_lo, r2_hi),
            'same_product_cold_start_videos': rng.randint(sc_lo, sc_hi),
        })
    return rows


def _m1_period_orders(rows, product, period, seed, extra_ratio=None):
    """生成七天商品订单合计：视频组订单加额外比例，保证不小于视频组订单。

    指定 extra_ratio 时使用商品级固定比例，否则按商品和周期独立抽样。"""
    group = sum(r['paid_orders'] for r in rows)
    lo, hi = SIM_PARAMS['off_group_order_ratio_range']
    if extra_ratio is None:
        rng = random.Random(_unit_seed_key(product['product_id'], period['period_id'],
                                           seed, '|M1'))
        extra_ratio = rng.uniform(lo, hi)
    return group + int(group * extra_ratio)


# ---- opview-1.3 新增单元（P11–P60 全部 8 期 + 既有 10 商品的 t0..t5）档位/模板参数 ----

def _legacy_params(product, period_id, seed, published):
    """为 P01–P10 的 t6/t7 使用 SIM_PARAMS，成交额效率由价格派生。"""
    rng = random.Random(_unit_seed_key(product['product_id'], period_id, seed))
    n = SIM_PARAMS['video_rows_per_product_period']
    vv_lo, vv_hi = SIM_PARAMS['vv_range']
    ctr_lo, ctr_hi = SIM_PARAMS['plc_ctr_range']
    d_lo, d_hi = SIM_PARAMS['days_after_publish_range']
    age_range = (d_lo, d_hi) if published else (COLD_START_MAX_DAY + 1, COLD_START_MAX_DAY + d_hi)
    return _gen_rows(rng, n, vv_lo, vv_hi, ctr_lo, ctr_hi, SIM_PARAMS['cvr'],
                     published, age_range, product['price'], period_id, product['product_id'])


def _new_unit_params(product, period_index, period_id, seed, published):
    """新增单元（或既有商品回溯期）：档位 + 模板锚点形状 + 局部随机流。

    锚点策略：t7 =档内固定锚（OC.t7_anchor_m2）；t0..t6 = t7 锚 × 模板形状系数
    （G6 前段再 ×OC._G6_T0_ANCHOR_RATIO 提档）；逐行 vv=期锚/行数×抖动，保证 sum(vv)≈期锚。
    ctr/cvr 按 G7/G8 两轴同设；其余用 SIM_PARAMS 默认带。
    """
    tier = OC.TIER[product['product_id']]
    tmpl = OC._TEMPLATE_MAIN.get(product['product_id'], 'G8')
    t7m = OC.t7_anchor_m2(tier)
    shape = OC._SHAPE[tmpl]
    g6 = (tmpl == 'G6')
    coeff = shape[period_index] * (OC._G6_T0_ANCHOR_RATIO if (g6 and period_index <= 3) else 1.0)
    target_m2_pre = max(1.0, (t7m * coeff) / MAGNITUDE_SCALE)

    tier_p = OC.MAGNITUDE_TIERS[tier]
    n = tier_p['rows']
    lo, hi = tier_p['vv_range']
    base = target_m2_pre / n
    # 档位内钳制：锚点要求的行均值超出档位带时取带端（避免跨档污染 分带）
    vv_lo, vv_hi = lo, hi
    if base < lo:
        vv_lo, vv_hi = lo, lo + int(0.1 * (hi - lo))
    elif base > hi:
        vv_lo, vv_hi = max(lo, int(hi * 0.9)), hi

    if tmpl == 'G7':
        ctr_lo, ctr_hi, cvr = OC.G7_CTR_RANGE[0], OC.G7_CTR_RANGE[1], OC.G7_CVR
    elif tmpl == 'G8':
        ctr_lo, ctr_hi, cvr = OC.G8_CTR_RANGE[0], OC.G8_CTR_RANGE[1], OC.G8_CVR
    else:
        ctr_lo, ctr_hi, cvr = SIM_PARAMS['plc_ctr_range'][0], SIM_PARAMS['plc_ctr_range'][1], tier_p['cvr']

    d_lo, d_hi = SIM_PARAMS['days_after_publish_range']
    age_range = (d_lo, d_hi) if published else (COLD_START_MAX_DAY + 1, COLD_START_MAX_DAY + d_hi)
    # 行 ctr 取该商品跨期稳定水平（带内位置由形状流抽一次），使 继承 vv 形状（见上）
    u_ctr, _ = _product_shape_draws(product['product_id'], seed)
    ctr_base = ctr_lo + (ctr_hi - ctr_lo) * u_ctr
    rng = random.Random(_unit_seed_key(product['product_id'], period_id, seed))
    rows = _gen_rows(rng, n, vv_lo, vv_hi, ctr_lo, ctr_hi, cvr,
                     published, age_range, product['price'], period_id,
                     product['product_id'], ctr_base=ctr_base)
    # 锚点校正：把行 vv 等比拉到 sum(vv)≈期锚（整数化；paid_orders/gpm/opm 随之重算，
    # 比率字段仍=派生值，保持 /自洽）
    s = sum(r['vv'] for r in rows)
    if s > 0:
        for r in rows:
            vv_new = max(0, int(round(r['vv'] * target_m2_pre / s)))
            if vv_new == 0:
                continue
            orders = max(1, int(vv_new * r['plc_ctr'] * cvr))
            r['vv'] = vv_new
            r['paid_orders'] = orders
            r['opm'] = round(orders / vv_new * 1000.0, 4)
            if r['gpm'] is not None:
                r['gpm'] = round(orders * product['price'] / vv_new * 1000.0, 4)
    return rows


def _inject_x3(rows, product_id, period_index, rec):
    """X3 零分母注入：记录级改写，不改随机流。"""
    spec = OC.X3_ZERO_DENOMINATOR.get(product_id)
    if not spec or spec['period_index'] != period_index:
        return
    kind = spec['kind']
    if kind == 'all_vv_zero':
        for r in rows:
            r['vv'] = 0
            r['paid_orders'] = 0
            r['opm'] = 0.0
            if r['gpm'] is not None:
                r['gpm'] = 0.0
        rec['period_orders'] = 0
    elif kind == 'all_plc_ctr_missing':
        for r in rows:
            r['plc_ctr'] = None
    elif kind == 'all_zero_record':
        for r in rows:
            r['vv'] = 0
            r['paid_orders'] = 0
            r['opm'] = 0.0
            if r['gpm'] is not None:
                r['gpm'] = 0.0
        rec['period_orders'] = 0


def _inject_x2(rows, product_id):
    """X2 缺指标注入：行字段置 None（不得用 0 代替）。"""
    specs = OC.X2_MISSING_FIELDS.get(product_id)
    if not specs:
        return
    for field, scope in specs:
        if scope == 'all':
            for r in rows:
                r[field] = None
        else:
            lo, hi = scope
            for i in range(lo, hi + 1):
                if i < len(rows):
                    rows[i][field] = None


def _apply_magnitude_scale(view):
    """输出前统一缩放播放量、行订单与商品订单，不改变随机流。

    比率保持原值，零分母结构与缺失标记保持不变。"""
    s = MAGNITUDE_SCALE
    for rec in view['records']:
        if rec.get('period_orders') is not None:
            rec['period_orders'] = rec['period_orders'] * s
        for row in rec['video_rows']:
            if row.get('vv') is not None:
                row['vv'] = row['vv'] * s
            if row.get('paid_orders') is not None:
                row['paid_orders'] = row['paid_orders'] * s
    view['magnitude_scale'] = s
    view['notes'].append(
        'opview-1.2 量级合同：绝对量字段（行 vv/paid_orders、记录 period_orders）=原始随机流生成值'
        ' × %d（MAGNITUDE_SCALE，显式常量，不进种子串）；比率/价格/天数/标记/行数不变；'
        '与 opview-1.1 基线（evidence/demo_qualification/baseline_view_v1_opview-1.1.json）的'
        '排名、比率、两期差异百分比、口径陷阱结构逐项一致（既有 10 商品 t6/t7 逐字节不变）' % s)
    return view


def build_operating_view(seed=DEFAULT_SEED):
    """生成商品 × 周期的模拟经营视图。

    正常单元一条记录，缺周期单元不生成，冲突单元保留多个变体。
    记录包含视频行与商品订单等基础量，指标由查询层按各自口径聚合。"""
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError('seed 必须是非负整数')
    objects, records = [], []
    for product in PRODUCTS:
        pid = product['product_id']
        objects.append({
            'product_id': pid,
            'product_name': product['product_name'],
            'category': product['category'],
            'city': product['city'],
            'price': product['price'],
        })
        x1_missing = set(OC.X1_MISSING_PERIODS.get(pid, ()))
        for period in PERIODS:
            pidx = period['period_index']
            if pidx in x1_missing:
                continue          # X1 缺周期：该 (商品,周期) 不出记录
            published = OC.published_flag(pid, pidx)
            legacy_unit = (pid in LEGACY_PRODUCT_IDS and pidx >= 6)
            if legacy_unit:
                rows = _legacy_params(product, period['period_id'], seed, published)
            else:
                rows = _new_unit_params(product, pidx, period['period_id'], seed, published)
            # X5/X6：跨期身份/属性漂移（记录层字段；objects 目录值保持主值）
            rec_city, rec_name = product['city'], product['product_name']
            x5 = OC.X5_IDENTITY_DRIFT.get(pid)
            if x5 and pidx < x5['period_index']:
                rec_name = x5['alt_name']
            x6 = OC.X6_ATTRIBUTE_DRIFT.get(pid)
            if x6 and x6['field'] == 'city' and pidx < x6['period_index']:
                rec_city = x6['alt_value']
            rec = {
                'record_id': 'rec:%s:%s' % (pid, period['period_id']),
                'product_id': pid,
                'product_name': rec_name,
                'category': product['category'],
                'city': rec_city,
                'price': product['price'],
                'published': published,
                'period_id': period['period_id'],
                'period_label': period['period_label'],
                'period_start_date': period['period_start_date'],
                'period_end_date': period['period_end_date'],
                'period_length_days': period['length_days'],
                'caliber_note': DEMO_CALIBER,
                'period_orders': _m1_period_orders(
                    rows, product, period, seed,
                    extra_ratio=None if legacy_unit
                    else _product_shape_draws(pid, seed)[1]),
                'video_rows': rows,
            }
            _inject_x3(rows, pid, pidx, rec)
            _inject_x2(rows, pid)
            records.append(rec)
            # X4 record conflict：同 (商品,周期) 追加一条指纹不同的记录（不择一/不平均由查询层判定）
            x4 = OC.X4_RECORD_CONFLICT.get(pid)
            if x4 and x4['period_index'] == pidx:
                dup = copy.deepcopy(rec)
                dup['record_id'] = 'rec:%s:%s#conflict' % (pid, period['period_id'])
                dup['period_orders'] = (rec['period_orders'] or 0) + 1
                records.append(dup)
    view = {
        'view_version': VIEW_VERSION,
        'generator_version': GENERATOR_VERSION,
        'seed': seed,
        'judge_date': JUDGE_DATE,
        'demo_caliber': DEMO_CALIBER,
        'periods': {p['period_id']: dict(p) for p in PERIODS},
        'period_ids': list(PERIOD_IDS),
        'objects': objects,
        'records': records,
        'params': copy.deepcopy(SIM_PARAMS),
        'param_reasons': copy.deepcopy(PARAM_REASONS),
        'notes': [
            '8 期数值由本视图生成规则产生，不来自真实平台或外部快照',
            'published=该期是否发布新增内容（非商品上下架）；跨期不同属正常 state_change，不是冲突',
            '本视图不含成本/利润/外部平台价/趋势/真实同比环比/因果证据',
            'opview-1.3：60 商品×8 周期的模拟经营数据，商品身份信息不代表真实经营结果',
        ],
    }
    return _apply_magnitude_scale(view)


def view_to_json(view, indent=1):
    """稳定序列化（键排序 + UTF-8）；供落盘留证与一致性核对，不参与运行路径。"""
    import json
    return json.dumps(view, ensure_ascii=False, sort_keys=True, indent=indent)


DAILY_VIEW_VERSION = 'opview-daily-1.0'
DAILY_GRANULARITY = 'day'


def date_range(start_date, end_date):
    """含端点的连续日期列表（ISO 字符串，升序；start>end 返回空）。纯函数。"""
    from datetime import date as _date, timedelta as _timedelta
    s = _date.fromisoformat(str(start_date))
    e = _date.fromisoformat(str(end_date))
    out = []
    while s <= e:
        out.append(s.isoformat())
        s += _timedelta(days=1)
    return out


def weekday_index(date_str):
    """ISO 日期 → 星期索引（0=周一 … 6=周日）。"""
    from datetime import date as _date
    return _date.fromisoformat(str(date_str)).weekday()


def _split_int(total, weights):
    """把非负整数 `total` 按 `weights` 分成 len(weights) 个非负整数，和**恰为** total。

    最大余数法（largest remainder）；权重为 0 的位次固定得 0（用于“休息日不出内容”）。
    `total is None` → 全 None（缺失语义原样保留，不当 0）。
    """
    n = len(weights)
    if total is None:
        return [None] * n
    if total <= 0:
        return [0] * n
    pos = [i for i, w in enumerate(weights) if w > 0]
    w = [float(x) for x in weights]
    if not pos:                     # 全 0 权重（防御）：退化为均分
        pos = list(range(n))
        w = [1.0] * n
    sw = sum(w[i] for i in pos)
    raw = {i: total * (w[i] / sw) for i in pos}
    out = [0] * n
    for i in pos:
        out[i] = int(raw[i])
    rem = total - sum(out)
    order = sorted(pos, key=lambda i: (-(raw[i] - out[i]), i))
    for k in range(rem):
        out[order[k % len(order)]] += 1
    return out


def _day_shape(product_id, dates, seed, kind):
    """一个自然周内的逐日形状权重（未归一）：星期形状 × 周内趋势 × 逐商品逐日轻噪声。

    kind ∈ {'supply','conversion'}（供给/流量 vs 成交转化两套星期形状，故两类指标日间
    起伏不互相冒充）。噪声由 (版本, 商品, 形状类型, 种子, 周内位次) 局部化，同输入可复现。
    """
    base = OC.DAILY_SUPPLY_WEEKDAY if kind == 'supply' else OC.DAILY_CONVERSION_WEEKDAY
    out = []
    for idx, dt in enumerate(dates):
        noise = random.Random('%s|%s|%s|%d|%d|shape' % (GENERATOR_VERSION, product_id, kind, seed, idx)) \
            .uniform(0.90, 1.10)
        out.append(base[weekday_index(dt)] * OC.DAILY_TREND[idx % len(OC.DAILY_TREND)] * noise)
    return out


def _day_row(src, product_id, period_id, date_str, row_index, part_index, vv, paid):
    """由周底层行 `src` 派生一条日行（同一底层行的第 part_index 份；跨日 video_id 唯一）。"""
    if paid is not None and vv:
        opm = round(paid / vv * 1000.0, 4)
    elif vv == 0 or (vv is not None and paid == 0):
        opm = 0.0
    else:
        opm = None
    return {
        'video_id': '%s-%s-%03d-%s-%d' % (str(period_id).replace('period_demo_', ''),
                                          str(product_id)[-4:], row_index + 1,
                                          str(date_str).replace('-', ''), part_index + 1),
        'days_after_publish': src.get('days_after_publish'),
        'is_public': src.get('is_public'),
        'has_valid_poi': src.get('has_valid_poi'),
        'has_commercial_tag': src.get('has_commercial_tag'),
        'has_violation_risk': src.get('has_violation_risk'),
        'in_cold_start': src.get('in_cold_start'),
        'breakout': src.get('breakout'),
        'vv': vv, 'gpm': src.get('gpm'), 'opm': opm, 'paid_orders': paid,
        'plc_ctr': src.get('plc_ctr'),
        'recent_2d_same_poi_posts': src.get('recent_2d_same_poi_posts'),
        'same_product_cold_start_videos': src.get('same_product_cold_start_videos'),
        'date': date_str,
    }


def _rest_heavy_plan(product_id, period_id, seed, n):
    """为 n 条底层行各定「休息日 / 加量日」：每日至少 1 行（n≥2 时成立）。

    m_i = 逐日条数向量（休息日 0、加量日 2、其余 1，Σ=7）；休息日按行错开模 7，加量日再偏移，
    使同一商品同一天既不恒为空、也不恒为满，逐日构成随日期变化（条均因此有日间差异）。
    """
    base = random.Random('%s|%s|%s|%d|rest' % (GENERATOR_VERSION, product_id, period_id, seed)).randrange(7)
    plan = []
    for i in range(n):
        rest = (base + i) % 7
        heavy = (rest + 3) % 7
        m = [1] * 7
        m[rest] = 0
        m[heavy] = 2
        plan.append(m)
    return plan


def _record_fingerprint(rec):
    """周记录内容指纹（X4 冲突判定＝同 (商品,周期) 存在内容互异的记录）。

    字段集与 `qctx_period._record_fingerprint` 同源（本模块不 import 查询层，故本地重列）。
    """
    import json
    payload = {k: rec.get(k) for k in ('product_id', 'product_name', 'category', 'city', 'price',
                                       'published', 'period_id', 'period_orders')}
    payload['video_rows'] = rec.get('video_rows')
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _split_record_to_days(rec, period, product, seed, variant, conflict=False):
    """既有周记录 → 7 条日记录（逐指标对账相等；variant 区分 X4 冲突副本）。

    `conflict=True`（该 (商品,周期) 存在互相矛盾的记录）时，7 天全部打上 `record_conflict`
    标记：冲突是**周记录身份**属性，不能因为某天两条变体拆分后日值恰好相同就丢掉冲突语义。
    """
    dates = date_range(period['period_start_date'], period['period_end_date'])
    pid = rec['product_id']
    rows = rec.get('video_rows') or []
    supply = _day_shape(pid, dates, seed, 'supply')
    conversion = _day_shape(pid, dates, seed, 'conversion')
    orders_by_day = _split_int(rec.get('period_orders'), supply)
    plan = _rest_heavy_plan(pid, period['period_id'], seed, len(rows))

    day_rows = {d: [] for d in range(len(dates))}
    for i, src in enumerate(rows):
        m = plan[i]
        w_vv = [supply[d] if m[d] > 0 else 0.0 for d in range(len(dates))]
        w_pd = [conversion[d] if m[d] > 0 else 0.0 for d in range(len(dates))]
        vv_parts = _split_int(src.get('vv'), w_vv)
        pd_parts = _split_int(src.get('paid_orders'), w_pd)
        for d in range(len(dates)):
            if m[d] <= 0:
                continue
            if m[d] == 1:
                sub_vv, sub_pd = [vv_parts[d]], [pd_parts[d]]
            else:
                sub_vv = _split_int(vv_parts[d], [1.0] * m[d])
                sub_pd = _split_int(pd_parts[d], [1.0] * m[d])
            for k in range(m[d]):
                day_rows[d].append(_day_row(src, pid, period['period_id'], dates[d], i, k,
                                            sub_vv[k], sub_pd[k]))

    daily = []
    for d, dt in enumerate(dates):
        daily.append({
            'product_id': pid,
            'product_name': rec.get('product_name'),
            'category': rec.get('category'),
            'city': rec.get('city'),
            'price': rec.get('price'),
            'published': rec.get('published'),
            'date': dt,
            'granularity': DAILY_GRANULARITY,
            'source_kind': 'week_split',
            'period_id': period['period_id'],
            'period_start_date': period['period_start_date'],
            'period_end_date': period['period_end_date'],
            'period_length_days': period['length_days'],
            'variant': variant,
            'record_conflict': bool(conflict),
            'period_orders': orders_by_day[d],
            'video_rows': day_rows[d],
            'caliber_note': rec.get('caliber_note') or DEMO_CALIBER,
        })
    return daily


def _earliest_week_level(recs):
    """取商品最早周期的周播放合计与订单，用作额外模拟日的量级锚点。"""
    if not recs:
        return None, None
    ordered = sorted(recs, key=lambda r: (r.get('period_start_date') or '', r['period_id']))
    first = ordered[0]
    m2 = sum(r.get('vv') for r in (first.get('video_rows') or []) if r.get('vv') is not None)
    return m2, first.get('period_orders')


def _simulate_new_day(product, date_str, seed, base_weekly_m2):
    """按最早周水平生成额外覆盖日，叠加星期形状与噪声。

    仅借用量级，不复制周记录值；此区间不额外注入缺失或冲突样本。"""
    pid = product['product_id']
    tier = OC.TIER[pid]
    tp = OC.MAGNITUDE_TIERS[tier]
    n = tp['rows']
    tmpl = OC._TEMPLATE_MAIN.get(pid, 'G8')
    if tmpl == 'G7':
        ctr_lo, ctr_hi, cvr = OC.G7_CTR_RANGE[0], OC.G7_CTR_RANGE[1], OC.G7_CVR
    elif tmpl == 'G8':
        ctr_lo, ctr_hi, cvr = OC.G8_CTR_RANGE[0], OC.G8_CTR_RANGE[1], OC.G8_CVR
    else:
        ctr_lo, ctr_hi, cvr = (SIM_PARAMS['plc_ctr_range'][0],
                               SIM_PARAMS['plc_ctr_range'][1], tp['cvr'])

    day = int(str(date_str)[-2:])
    lo_t, hi_t = OC.MARCH_DAILY_TREND
    march_trend = lo_t + (hi_t - lo_t) * ((day - 1) / 30.0)     # 3 月内部自月初到月末的轻微上行
    level = OC.MARCH_BASE_LEVEL * march_trend
    shape_rng = random.Random('%s|%s|%s|new-shape' % (GENERATOR_VERSION, pid, date_str))
    day_factor = OC.DAILY_SUPPLY_WEEKDAY[weekday_index(date_str)] * shape_rng.uniform(0.90, 1.10)
    if base_weekly_m2 is None:
        base_weekly_m2 = OC.t7_anchor_m2(tier)
    target_m2 = max(float(n), float(base_weekly_m2) / 7.0 * level * day_factor)

    u_ctr, extra_ratio = _product_shape_draws(pid, seed)
    ctr_base = ctr_lo + (ctr_hi - ctr_lo) * u_ctr
    rng = random.Random('%s|%s|%s|%d|new' % (GENERATOR_VERSION, pid, date_str, seed))
    vv_split = _split_int(int(round(target_m2)), [1.0 + rng.random() for _ in range(n)])

    rows = []
    for i in range(n):
        vv = vv_split[i]
        ctr = round(min(ctr_hi, max(ctr_lo, ctr_base * (0.9 + 0.2 * rng.random()))), 4)
        orders = max(0, int(vv * ctr * cvr))
        gpm = round(orders * product['price'] / vv * 1000.0, 4) if vv else 0.0
        opm = round(orders / vv * 1000.0, 4) if vv else 0.0
        days = rng.randint(*SIM_PARAMS['days_after_publish_range'])
        rows.append({
            'video_id': 'new-%s-%03d-%s' % (str(pid)[-4:], i + 1, str(date_str).replace('-', '')),
            'days_after_publish': days, 'is_public': rng.random() < SIM_PARAMS['public_p'],
            'has_valid_poi': rng.random() < SIM_PARAMS['poi_p'],
            'has_commercial_tag': rng.random() < SIM_PARAMS['commercial_p'],
            'has_violation_risk': rng.random() < SIM_PARAMS['violation_p'],
            'in_cold_start': days <= COLD_START_MAX_DAY,
            'breakout': rng.random() < SIM_PARAMS['breakout_p'],
            'vv': vv, 'gpm': gpm, 'opm': opm, 'paid_orders': orders, 'plc_ctr': ctr,
            'recent_2d_same_poi_posts': rng.randint(*SIM_PARAMS['recent_2d_same_poi_posts_range']),
            'same_product_cold_start_videos': rng.randint(*SIM_PARAMS['same_product_cold_start_videos_range']),
            'date': date_str,
        })
    group = sum(r['paid_orders'] for r in rows)
    orders_day = group + int(group * extra_ratio * rng.uniform(0.95, 1.05))
    return {
        'product_id': pid, 'product_name': product['product_name'], 'category': product['category'],
        'city': product['city'], 'price': product['price'], 'published': True,
        'date': date_str, 'granularity': DAILY_GRANULARITY, 'source_kind': 'new_simulated',
        'period_id': None, 'period_start_date': None, 'period_end_date': None,
        'period_length_days': 1, 'variant': 0, 'record_conflict': False,
        'period_orders': orders_day,
        'video_rows': rows, 'caliber_note': DEMO_CALIBER,
    }


def build_daily_view(seed=DEFAULT_SEED):
    """生成覆盖 2026-03-01 ~ 2026-05-27 的「商品 × 日期」日级事实视图（opview-daily-1.0）。

    返回 dict：daily_view_version/coverage/…/days（每条＝一个 (商品, 日期, 变体) 日事实）。
    既有 8 周由周记录确定性展开（对账相等），新增日为独立模拟；均带 demo 口径说明。
    """
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError('seed 必须是非负整数')
    weekly = build_operating_view(seed)
    by_key = {}
    for rec in weekly['records']:
        by_key.setdefault((str(rec['product_id']), rec['period_id']), []).append(rec)

    days = []
    for product in PRODUCTS:
        pid = product['product_id']
        product_recs = [r for r in weekly['records'] if str(r['product_id']) == pid]
        for period in PERIODS:
            recs = by_key.get((pid, period['period_id']), [])
            if not recs:                       # X1 缺周期：该周不出日事实
                continue
            conflict = len({_record_fingerprint(r) for r in recs}) > 1   # X4：冲突身份随周保留
            for variant, rec in enumerate(recs):
                days.extend(_split_record_to_days(rec, period, product, seed, variant,
                                                  conflict=conflict))
        base_weekly_m2, _ = _earliest_week_level(product_recs)
        for dt in date_range(OC.DATA_COVERAGE_START, '2026-04-01'):
            days.append(_simulate_new_day(product, dt, seed, base_weekly_m2))
    days.sort(key=lambda d: (d['date'], d['product_id'], d['variant']))
    return {
        'daily_view_version': DAILY_VIEW_VERSION,
        'generator_version': GENERATOR_VERSION,
        'week_view_version': VIEW_VERSION,
        'seed': seed,
        'coverage_start': OC.DATA_COVERAGE_START,
        'coverage_end': OC.DATA_COVERAGE_END,
        'demo_business_date': OC.DEMO_BUSINESS_DATE,
        'latest_complete_date': OC.LATEST_COMPLETE_DATE,
        'demo_caliber': DEMO_CALIBER,
        'days': days,
        'notes': [
            '日级事实是既有周记录（opview-1.3）的确定性日级展开（既有 8 周）+ 新增独立模拟日'
            '（2026-03-01~2026-04-01）；展开前后逐商品逐指标对账相等（M1 合计、M2 sum(vv)、'
            'M3 条均、M4/M5 分子分母与 eligible 配对行全部回到原周口径）',
            '日间差异来自供给/转化两套星期形状与周内趋势（operating_catalog.DAILY_*），'
            '不是“周总量除以 7”；X1 缺周期不出日事实；X2 缺失字段/None 逐日保留（缺失≠0）；'
            'X3 零分母结构逐日保留；X4 同键双记录 → 每天两条互异日记录',
            '“昨天”＝%s（模拟当前日 %s）；全部数值为 Demo 模拟，非平台真实日期或线上效果'
            % (OC.LATEST_COMPLETE_DATE, OC.DEMO_BUSINESS_DATE),
        ],
    }
