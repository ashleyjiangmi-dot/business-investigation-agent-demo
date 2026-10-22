# -*- coding: utf-8 -*-
"""共享经营口径、周期能力说明与商品选项投影。

从 period_view_generator 的商品目录生成可读选项与标签映射，按商品名保序去重。
业务说明与请求形状为确定性文本，不提供答案或安排查询顺序。
本模块不读取快照、评测索引或外部服务。"""
import hashlib
import json
import os
import sys

from . import period_view_generator as PVG  # noqa: E402

SHARED_BUSINESS_NOTE_VERSION = 'oi03-shared-note-1.0'
PERIOD_BRIEF_VERSION = 'oi03-period-brief-1.0'
ENTRY_CONTRACT_VERSION = 'pctx_entry_oi03_v2'

OPTION_ALL = '全部商品（%d 个，目录集合）' % len(PVG.PRODUCTS)
PERIOD_CHOICE_P2 = '本周期（period_demo_p2）'
PERIOD_CHOICE_P1 = '上一周期（period_demo_p1）'
PERIOD_CHOICE_BOTH = '两期对比'
PERIOD_CHOICES = (PERIOD_CHOICE_P2, PERIOD_CHOICE_P1, PERIOD_CHOICE_BOTH)

# 周期查询操作面（pctx_v1 已实现；catalog 不开放给规划器，作为服务面自省保留）
PROPOSER_OPS = ('value', 'diff', 'set_compare', 'states')
PERIOD_QUERY_KEYS = ('op', 'product_ids', 'period_id', 'metrics', 'metric', 'kind',
                     'base_period_id', 'target_period_id')


def shared_business_note():
    """ 共享默认业务说明（对模型可见，与判据同义）。"""
    return (
        '共享默认业务说明（%s；与判据同义，随输入原样交付）：\n'
        '1. 「同集合水平」= 所选集合在本周期内该指标的**中位数（median）**；高于=值>中位数，'
        '低于=值<中位数，等于中位数单列「持平」；无值或分母为 0 的对象**不进比较**，计入缺失组。\n'
        '2. 「卖得怎么样」默认必需指标 = **订单量（M1）与播放量（M2）**；须给出两项的两期值、'
        '绝对差与百分比差，并说明两期是否可比。\n'
        '本说明只给被钉死的口径定义，不排查询顺序、不给工具名、不给答案，也不给阈值之外的推断。'
        % SHARED_BUSINESS_NOTE_VERSION)


def period_brief():
    """pctx_v1 能力/请求形状说明（供规划器注入；只描述已实现能力）。"""
    periods = '；'.join('%s=%s %s~%s（%d 天）'
                       % (p['period_id'], p['period_label'], p['period_start_date'],
                          p['period_end_date'], p['length_days']) for p in PVG.PERIODS)
    metrics = '；'.join('%s %s（%s，%s）' % (k, v['name'], v['unit'], v['role'])
                       for k, v in (('M1', {'name': '订单量', 'unit': '单', 'role': '结果'}),
                                    ('M2', {'name': '播放量', 'unit': '次', 'role': '过程'}),
                                    ('M3', {'name': '点击率', 'unit': '比率（条均，非曝光加权）', 'role': '过程'}),
                                    ('M4', {'name': '千次播放订单 OPM', 'unit': '单/千次播放', 'role': '结果'}),
                                    ('M5', {'name': '千次播放成交额 GPM', 'unit': '元/千次播放（VV 加权）', 'role': '结果'})))
    return (
        'pctx_v1 周期查询能力说明（%s）：\n'
        '[1] 周期：%s。周期是固定模拟周期，不提供真实同比/环比、任意日期滚动或连续趋势。\n'
        '[2] 对象：只用目录内 product_id（%d 个，见目录，可读名与选择标签一致）。'
        'snapshot_id/case_id 属**旧命名空间**，与本周期命名空间互斥，混用=namespace_conflict。\n'
        '[3] 指标：%s。\n'
        '[4] 操作：value（对象集合×周期×指标取值）；diff（同对象同指标 target−base 与百分比差，'
        '需 base_period_id/target_period_id）；set_compare（单周期内同集合排名或中位数基准，'
        'kind=median|rank）；states（对象身份/状态/属性/记录状态）。\n'
        '[5] 请求形状：{"op":"value","product_ids":[...],"period_id":"...","metrics":["M1"]}；'
        'diff 用 {"op":"diff","product_ids":[...],"metric":"M1","base_period_id":"...",'
        '"target_period_id":"..."}；set_compare 用 {"op":"set_compare","product_ids":[...],'
        '"period_id":"...","metric":"M4","kind":"median"}；states 用 {"op":"states","product_ids":[...]}。\n'
        '    允许的键只有 %s；出现其它键=未知键，一律确定性拒绝。查询对象只来自本次显式选中范围。\n'
        '[6] 缺失与零分母：unknown 一律附原因码（zero_denominator / period_missing / field_missing / '
        'window_incomparable），不静默代 0；不返回 *_level / round_verdict / 动作建议。'
        % (PERIOD_BRIEF_VERSION, periods, len(PVG.PRODUCTS), metrics, '、'.join(PERIOD_QUERY_KEYS)))


# 品类快捷选择标签（前缀在此单点现算；页面只消费下发字符串，不另造第二套品类目录）。
CATEGORY_LABEL_PREFIX = '品类 · '


def category_scope_label(category):
    """品类 → 入口选择标签（确定性；空品类返回空串，由调用方跳过）。"""
    cat = str(category or '').strip()
    return '%s%s' % (CATEGORY_LABEL_PREFIX, cat) if cat else ''


def category_options():
    """目录品类去重的入口选项标签，按**目录首次出现顺序**（不排序、不写死品类名）。

    入口只认这一处下发的标签；成员集合由同一目录的 `category` 现算（core/entry_adapter），
    不在前端或另处维护第二份品类目录。
    """
    seen = []
    for p in PVG.PRODUCTS:
        cat = str(p.get('category') or '').strip()
        if cat and cat not in seen:
            seen.append(cat)
    return [category_scope_label(c) for c in seen]


def entry_options():
    """原生入口选项投影：商品名去重、保持身份表顺序；附范围选项。"""
    names = [p['product_name'] for p in PVG.PRODUCTS]
    deduped = []
    for n in names:
        if n not in deduped:
            deduped.append(n)
    return {'product_options': [OPTION_ALL] + deduped,
            'period_options': list(PERIOD_CHOICES),
            'product_count': len(deduped),
            'note': '可读商品名标量选择（去重 %d 项）；不展示 26 个案例，不假定多选或搜索' % len(deduped)}


def product_scope_labels():
    """product_id→入口/展示标签（同名消歧规则的唯一定义处）。

    目录里存在同名商品（如两款「抹茶吐司」＝鲍师傅糕点 / UH 祐禾）且品牌非空时，标签为
    「名称（品牌）」；其余商品标签＝名称原样（不改动既有可提交值）。入口下发选项、
    label→id 映射与前端展示都只认这一处，不在任何消费面另算第二套口径。
    """
    counts = {}
    for p in PVG.PRODUCTS:
        counts[p['product_name']] = counts.get(p['product_name'], 0) + 1
    out = {}
    for p in PVG.PRODUCTS:
        name, brand = p['product_name'], p.get('brand') or ''
        out[p['product_id']] = '%s（%s）' % (name, brand) if counts[name] > 1 and brand else name
    return out


def label_to_product_id(label):
    """选择标签→product_id（确定性）。接受可读名、同名消歧标签与原始 product_id 回放；未知= None。"""
    s = str(label or '').strip()
    labels = product_scope_labels()
    for p in PVG.PRODUCTS:
        if s == labels[p['product_id']] or s == p['product_name'] or s == p['product_id']:
            return p['product_id']
    return None


def all_product_ids():
    return [p['product_id'] for p in PVG.PRODUCTS]


def entry_catalog():
    """投影商品身份、目录价、消歧标签与周期目录，供入口与任务输入共用。

    目录价为模拟数据，与成交额计算使用同一价格口径。"""
    labels = product_scope_labels()
    return {
        'view_version': PVG.VIEW_VERSION,
        'periods': [dict(p) for p in PVG.PERIODS],
        'products': [{'product_id': p['product_id'], 'product_name': p['product_name'],
                      'brand': p.get('brand') or '', 'category': p['category'], 'city': p['city'],
                      'price': p.get('price'),
                      'scope_label': labels[p['product_id']]} for p in PVG.PRODUCTS],
        'demo_caliber': PVG.DEMO_CALIBER,
    }


def _hash_text(s):
    return hashlib.sha256(str(s).encode('utf-8')).hexdigest()


def brief_hash():
    return {'shared_business_note': _hash_text(shared_business_note()),
            'period_brief': _hash_text(period_brief()),
            'versions': {'shared_business_note': SHARED_BUSINESS_NOTE_VERSION,
                         'period_brief': PERIOD_BRIEF_VERSION,
                         'entry_contract': ENTRY_CONTRACT_VERSION}}


def brief_json():
    return json.dumps({'shared_business_note': shared_business_note(),
                       'period_brief': period_brief(),
                       'entry_options': entry_options(),
                       'catalog': entry_catalog(),
                       'versions': brief_hash()['versions']}, ensure_ascii=False, sort_keys=True)


__all__ = ['SHARED_BUSINESS_NOTE_VERSION', 'PERIOD_BRIEF_VERSION', 'ENTRY_CONTRACT_VERSION',
           'OPTION_ALL', 'PERIOD_CHOICES', 'PROPOSER_OPS',
           'PERIOD_QUERY_KEYS', 'CATEGORY_LABEL_PREFIX', 'shared_business_note', 'period_brief',
           'entry_options', 'category_scope_label', 'category_options',
           'label_to_product_id', 'product_scope_labels', 'all_product_ids', 'entry_catalog',
           'brief_hash', 'brief_json']
