# -*- coding: utf-8 -*-
"""MCP 查询工具的名称、描述、参数 schema 与请求转换。

四个工具分别映射取值、跨期比较、集合比较与条件筛选；服务端通过
ToolServerClient.query 执行同一数据查询合同。运行身份由可信调用方注入，
不接受模型更换根任务；参数类型在边界检查，业务取值由数据层判定。"""
from __future__ import annotations

import copy

from core.data import qctx_period as QP

# ---- 工具 ↔ 固定 op（唯一映射；MCP 层不得改写成别的 op）----
# `filter_products` 是**第四个**原生 MCP Tool，固定映射既有 `op=cohort_filter`
# （不新建 Server、不重写判定；筛选语义与证据全走既有数据层单点）。
TOOL_OPS = {
    'get_metric_values': 'value',
    'compare_periods': 'diff',
    'compare_product_set': 'set_compare',
    'filter_products': 'cohort_filter',
}

# ---- 数据层真实能力（单点引用，不手写副本）----
METRIC_ENUM = list(QP.METRIC_KEYS)              # M1–M5
SUM_METRIC_ENUM = list(QP.AGGREGATE_METRICS)    # 可加总：M1/M2
SUM_AGGREGATE_VALUES = list(QP.AGGREGATE_VALUES)  # ('sum',)
AGGREGATE_MIN_PRODUCTS = QP.AGGREGATE_MIN_PRODUCTS  # 少于 2 个唯一商品不发合计

# ---- 时间字段全集（三种写法互斥校验用）----
SINGLE_TIME_GROUPS = (('period_id',), ('window',), ('time',))
PAIR_TIME_GROUPS = (('base_period_id', 'target_period_id'),
                    ('base_window', 'target_window'),
                    ('time',))
_ALL_TIME_KEYS = ('period_id', 'window', 'time', 'base_period_id', 'target_period_id',
                  'base_window', 'target_window')

# 时间槽内的九种形态（周期形态 which/offset；日期形态七种）——与
# period_resolver.PERIOD_FORM_KEYS / DATE_FORM_KEYS 同集合（测试钉住）。
SLOT_FORM_KEYS = ('which', 'offset', 'days_ago', 'weeks_ago', 'date', 'date_range',
                  'last_days', 'month', 'month_to_date')
_MONTH_PATTERN = '^(this|last|[0-9]{4}-[0-9]{2})$'


def _date_range(description):
    return {
        'type': 'object',
        'description': description,
        'properties': {
            'start_date': {'type': 'string', 'format': 'date',
                           'description': '首日 YYYY-MM-DD（含）。'},
            'end_date': {'type': 'string', 'format': 'date',
                         'description': '末日 YYYY-MM-DD（含）。'},
        },
        'required': ['start_date', 'end_date'],
        'additionalProperties': False,
    }


def time_slot():
    """一个时间槽：目录周期形态（which / offset 二选一）或日期形态（七选一）。"""
    return {
        'type': 'object',
        'description': '一个时间槽：目录周期形态（which 或 offset）或日期形态'
                       '（days_ago/weeks_ago/date/date_range/last_days/month/month_to_date '
                       '恰给一种）；真实日期由服务端按日级覆盖解析，不在工具层换算。',
        'properties': {
            'which': {'type': 'string', 'enum': ['latest', 'previous', 'two_ago'],
                      'description': 'latest=最新期、previous=上一期、two_ago=上上期'
                                     '（锚=本轮允许周期中目录日期最晚一期）。'},
            'offset': {'type': 'integer', 'maximum': 0,
                       'description': '0=最新期、-1=上一期、-N=向过去回溯 N 期。'},
            'days_ago': {'type': 'integer', 'minimum': 0,
                         'description': '以模拟业务日为锚回推 N 天的单个自然日'
                                        '（1=昨天；0=业务日当天落到最新完整日）。'},
            'weeks_ago': {'type': 'integer', 'minimum': 0,
                          'description': '以最新完整日为锚回推整周的同一天（1=上周同一天）。'},
            'date': {'type': 'string', 'format': 'date', 'description': '显式单个自然日。'},
            'date_range': _date_range('显式日期区间（含首末两日）。'),
            'last_days': {'type': 'integer', 'minimum': 1,
                          'description': '以最新完整日为末日的最近 N 天窗口（含末日）。'},
            'month': {'type': 'string', 'pattern': _MONTH_PATTERN,
                      'description': '完整自然月：this=业务日所在月、last=上一个自然月、'
                                     'YYYY-MM=指定月；当月未完结只取已覆盖部分。'},
            'month_to_date': {'type': 'string', 'pattern': _MONTH_PATTERN,
                              'description': '月内同进度区间（与最新完整日同「日」对齐）。'},
        },
        'oneOf': [{'required': [key]} for key in SLOT_FORM_KEYS],
        'additionalProperties': False,
    }


def time_single():
    """单期时间语义：只给 `period` 槽。"""
    return {
        'type': 'object',
        'description': '单期相对时间语义：只给 period 槽，槽内二选一——目录周期形态'
                       '（which/offset）或日期形态；真实身份由服务端解析。',
        'properties': {'period': time_slot()},
        'required': ['period'],
        'additionalProperties': False,
    }


def time_pair():
    """期间对时间语义：`base`（较早）与 `target`（较晚）都必给。"""
    return {
        'type': 'object',
        'description': '期间对相对时间语义：base 与 target 两个槽都必给，槽内二选一'
                       '（周期形态或日期形态）；base 须早于 target（按真实日期判断）。',
        'properties': {'base': time_slot(), 'target': time_slot()},
        'required': ['base', 'target'],
        'additionalProperties': False,
    }


def _time_exclusive(groups):
    """三种时间写法互斥：恰给一组，且不得出现其它组的任一键。"""
    branches = []
    for keys in groups:
        foreign = [{'required': [k]} for k in _ALL_TIME_KEYS if k not in keys]
        branches.append({'required': list(keys), 'not': {'anyOf': foreign}})
    return copy.deepcopy(branches)


def _aggregate_guard():
    """显式请求 aggregate 时，仅允许可加总指标；普通查询不受该聚合约束。"""
    return {
        'if': {'required': ['aggregate']},
        'then': {'properties': {'metrics': {'items': {'enum': list(SUM_METRIC_ENUM)}}}},
    }


def _aggregate_schema():
    return {
        'type': 'string',
        'enum': list(SUM_AGGREGATE_VALUES),
        'description': '可选：对本次商品集合 × 指标 × 单期时间做合计，仅 M1（订单量）/'
                       'M2（播放量）可加总；缺省＝不合计（只给逐商品读数）。给 aggregate 时 '
                       'metrics 只能含 M1/M2（Schema 层即拒绝 M3/M4/M5，不进入数据层）。'
                       '合计需至少 %d 个不同商品才产出；不足时响应不含 single_period_totals，'
                       '逐商品读数照常返回。' % AGGREGATE_MIN_PRODUCTS,
    }


def _product_ids(description):
    return {'type': 'array', 'minItems': 1, 'items': {'type': 'string'},
            'description': description}


def _metric(description):
    return {'type': 'string', 'enum': list(METRIC_ENUM), 'description': description}


def scope_schema():
    """定义共享业务范围 scope；get_metric_values 必须提供该范围。"""
    return {
        'type': 'object',
        'description': '业务范围：全池 / 一个或多个目录品类 / 显式商品 ID 集合；三种形态恰给一种。'
                       '品类成员由服务端从可信目录解析，不需要也不允许模型枚举成员。',
        'properties': {
            'kind': {'type': 'string', 'enum': list(QP.SCOPE_KINDS),
                     'description': 'benchmark_all=本轮可调查范围全池；category=一个或多个目录品类；'
                                    'product_ids=显式商品 ID 集合（单商品为长度 1 的特例）。'},
            'categories': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                           'items': {'type': 'string'},
                           'description': '目录品类名，可带入口标签前缀「品类 · 」。只接受目录真实品类，'
                                          '不接受自造名、别名或模糊匹配。仅 kind=category 使用。'},
            'product_ids': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                            'items': {'type': 'string'},
                            'description': '商品 ID 列表（取自本轮下发商品目录）；重复 ID 直接拒绝，'
                                           '不静默去重。仅 kind=product_ids 使用。'},
        },
        'required': ['kind'],
        'allOf': [
            {'if': {'properties': {'kind': {'const': 'benchmark_all'}}, 'required': ['kind']},
             'then': {'not': {'anyOf': [{'required': ['categories']},
                                        {'required': ['product_ids']}]}}},
            {'if': {'properties': {'kind': {'const': 'category'}}, 'required': ['kind']},
             'then': {'required': ['categories'], 'not': {'required': ['product_ids']}}},
            {'if': {'properties': {'kind': {'const': 'product_ids'}}, 'required': ['kind']},
             'then': {'required': ['product_ids'], 'not': {'required': ['categories']}}},
        ],
        'additionalProperties': False,
    }


def _group_by_guard():
    """`group_by` 只接受 category，且必须与 `aggregate='sum'` 同时给（数据层同一纪律）。"""
    return {
        'if': {'required': ['group_by']},
        'then': {'required': ['aggregate']},
    }


def _group_by_schema():
    return {
        'type': 'string', 'enum': list(QP.GROUP_BY_VALUES),
        'description': '可选：按目录品类分组给合法合计与成员贡献（仅 category）。必须同时给 '
                       'aggregate="sum"；只支持 M1/M2（aggregate 已约束），不把成员合并后'
                       '冒称某个品类总量。缺省＝不发 category_totals/contribution。',
    }


def get_metric_values_schema():
    """`get_metric_values` → `op=value`。"""
    return {
        'type': 'object',
        'properties': {
            'scope': scope_schema(),
            'metrics': {
                'type': 'array', 'minItems': 1, 'uniqueItems': True,
                'items': {'type': 'string', 'enum': list(METRIC_ENUM)},
                'description': '要读取的指标，可多指标：M1 订单量、M2 播放量、M3 点击率、'
                               'M4 千次播放订单（OPM）、M5 千次播放成交额（GPM）。',
            },
            'period_id': {'type': 'string',
                          'description': '目录周期 ID（如 period_demo_p2），显式周期形态；'
                                         '与 window、time 三选一。'},
            'window': _date_range('单期日期窗口（含首末两日，单日窗口 start_date=end_date）；'
                                  '与 period_id、time 三选一。'),
            'time': time_single(),
            'aggregate': _aggregate_schema(),
            'group_by': _group_by_schema(),
        },
        'required': ['scope', 'metrics'],
        'oneOf': _time_exclusive(SINGLE_TIME_GROUPS),
        'allOf': [_aggregate_guard(), _group_by_guard()],
        'additionalProperties': False,
    }


def _metric_form_guard():
    """要求 compare_periods 在 metrics 与 metric 中恰选一种。

    指标互斥条件置于 allOf，与时间形态的 oneOf 分开，避免合法请求同时命中两种分支而被拒绝。"""
    return {
        'oneOf': [
            {'required': ['metrics'], 'not': {'required': ['metric']}},
            {'required': ['metric'], 'not': {'required': ['metrics']}},
        ],
    }


def compare_periods_schema():
    """`compare_periods` → `op=diff`（共享范围 + 多指标 `metrics[]`）。"""
    return {
        'type': 'object',
        'properties': {
            'scope': scope_schema(),
            'metrics': {
                'type': 'array', 'minItems': 1, 'maxItems': 5, 'uniqueItems': True,
                'items': {'type': 'string', 'enum': list(METRIC_ENUM)},
                'description': '要比较的指标，可一次多指标：M1 订单量、M2 播放量、M3 点击率、'
                               'M4 千次播放订单（OPM）、M5 千次播放成交额（GPM）。多指标时响应'
                               '按 by_metric 逐指标给两期值、绝对差、变化率与可比状态，指标之间'
                               '不合并判断。与旧写法 metric 互斥，只给一种。',
            },
            'metric': _metric('单一指标（旧写法，与 metrics 互斥，只给一种）：跨期比较每次只比'
                              '一个指标（M1 订单量、M2 播放量、M3 点击率、M4 OPM、M5 GPM）。'),
            'base_period_id': {'type': 'string',
                               'description': '较早一期的目录周期 ID；与 target_period_id 成对，'
                                              '且与日期窗口对、time 三选一。'},
            'target_period_id': {'type': 'string',
                                 'description': '较晚一期的目录周期 ID；须晚于 base_period_id。'},
            'base_window': _date_range('较早一侧的日期窗口（含首末两日）；与 target_window 成对，'
                                       '且与周期对、time 三选一。'),
            'target_window': _date_range('较晚一侧的日期窗口（含首末两日），须晚于 base_window。'),
            'time': time_pair(),
        },
        'required': ['scope'],
        'oneOf': _time_exclusive(PAIR_TIME_GROUPS),
        'allOf': [_metric_form_guard()],
        'additionalProperties': False,
    }


def compare_product_set_schema():
    """`compare_product_set` → `op=set_compare`（共享范围）。"""
    return {
        'type': 'object',
        'properties': {
            'scope': scope_schema(),
            'metric': _metric('单一指标：集合比较每次只比一个指标。'),
            'kind': {'type': 'string', 'enum': ['rank', 'median'],
                     'description': '比较基准：rank=集合内排名；median=集合中位数基准'
                                    '（高于/持平/低于中位数）。须显式声明，不默认取其一。'},
            'period_id': {'type': 'string',
                          'description': '目录周期 ID，显式周期形态；与 window、time 三选一。'},
            'window': _date_range('单期日期窗口（含首末两日，单日窗口 start_date=end_date）；'
                                  '与 period_id、time 三选一。'),
            'time': time_single(),
        },
        'required': ['scope', 'metric', 'kind'],
        'oneOf': _time_exclusive(SINGLE_TIME_GROUPS),
        'additionalProperties': False,
    }


# ===================== `filter_products`（原生 MCP 入口 → op=cohort_filter） =====================
# 条件闭集直接引用数据层单点（`core.data.qctx_period`），不在本包手抄第二份枚举/上限：
COHORT_CONDITION_KEYS = QP.COHORT_CONDITION_KEYS
COHORT_OPERATORS = QP.COHORT_OPERATORS
COHORT_MAX_CONDITIONS = QP.COHORT_MAX_CONDITIONS
COHORT_FIELDS = (QP.COHORT_FIELD_SINGLE, *QP.COHORT_FIELDS_PAIR)
# filter_products 只接受单期周期、双期周期对或相对周期；
# 数据层 COHORT_REQUEST_KEYS 不含日期窗口键。
FILTER_TIME_GROUPS = (('period_id',), ('base_period_id', 'target_period_id'), ('time',))


def time_slot_period_only():
    """只接受 which/offset 的周期时间槽；filter_products 不支持日期窗口形态。"""
    slot = time_slot()
    props = slot['properties']
    slot['properties'] = {key: props[key] for key in ('which', 'offset')}
    slot['oneOf'] = [{'required': [key]} for key in ('which', 'offset')]
    slot['description'] = ('一个目录周期槽位：which 与 offset 二选一。'
                           '本工具只用目录周期形态，不接受日期或窗口形态。')
    return slot


def time_filter():
    """`filter_products` 的相对时间语义：单期 `period` 槽 / 双期 `base`+`target` 槽。

    两个槽都只用周期形态；单双期互斥（`oneOf` 两支互斥必填）。

    三个槽各自新建一份槽位 dict：`time_slot_period_only()` 返回的是可变结构，共用同一
    对象会让任一侧后续就地改写串到另外两槽（Schema 是合同单点，必须每侧独立）。
    """
    return {
        'type': 'object',
        'description': '相对时间语义：单期用 period 槽，双期用 base 与 target 槽（base 较早）；'
                       '槽内只用目录周期形态（which/offset）。',
        'properties': {'period': time_slot_period_only(),
                       'base': time_slot_period_only(),
                       'target': time_slot_period_only()},
        'oneOf': [
            {'required': ['period'],
             'not': {'anyOf': [{'required': ['base']}, {'required': ['target']}]}},
            {'required': ['base', 'target'], 'not': {'required': ['period']}},
        ],
        'additionalProperties': False,
    }


def _condition_item_schema():
    """一条数值条件（平面 AND，无嵌套/OR/NOT）。"""
    return {
        'type': 'object',
        'description': '一条数值条件：指标 + 判定字段 + 比较算子 + 阈值。',
        'properties': {
            'metric': _metric('条件针对的指标（M1 订单量、M2 播放量、M3 点击率、'
                              'M4 千次播放订单、M5 千次播放成交额）。'),
            'field': {'type': 'string', 'enum': list(COHORT_FIELDS),
                      'description': '判定字段：单期用 value（该期取值）；双期用 absolute_diff'
                                     '（绝对差）或 percent_diff（百分比差，同一期间对可混用）。'
                                     '字段必须与时间模式匹配，不匹配由数据层拒绝。'},
            'operator': {'type': 'string', 'enum': list(COHORT_OPERATORS),
                         'description': '比较算子：< <= == != >= >。'},
            'value': {'type': 'number',
                      'description': '阈值（有限数值）。阈值由本次问题给定，本工具不内置'
                                     '「值得加量」「明显下滑」之类经营默认阈值。'},
        },
        'required': ['metric', 'field', 'operator', 'value'],
        'additionalProperties': False,
    }


def filter_products_schema():
    """`filter_products` → `op=cohort_filter`。

    范围用共享 `scope`（全池 / 一个或多个目录品类 / 显式商品集合）；条件 1–4 条平面 AND；
    时间三选一且只用目录周期形态。判定与分母全部由数据层既有 `cohort_filter` 单点决定。
    """
    return {
        'type': 'object',
        'properties': {
            'scope': scope_schema(),
            'conditions': {
                'type': 'array', 'minItems': 1, 'maxItems': COHORT_MAX_CONDITIONS,
                'items': _condition_item_schema(),
                'description': '1–%d 条 AND 条件（平面，无嵌套/OR/NOT）。范围内逐商品求值求交：'
                               '任一条件为假即不命中，缺数按「无法判定」单列（不代 0），'
                               '响应给出命中/不命中/无法判定三组与实际分母。' % COHORT_MAX_CONDITIONS,
            },
            'period_id': {'type': 'string',
                          'description': '目录周期 ID（单期形态）；与 base_period_id/target_period_id、'
                                         'time 三选一。'},
            'base_period_id': {'type': 'string',
                               'description': '较早一期的目录周期 ID（双期形态，须与 '
                                              'target_period_id 成对）。'},
            'target_period_id': {'type': 'string',
                                 'description': '较晚一期的目录周期 ID（双期形态，须与 '
                                                'base_period_id 成对）。'},
            'time': time_filter(),
        },
        'required': ['scope', 'conditions'],
        'oneOf': _time_exclusive(FILTER_TIME_GROUPS),
        'additionalProperties': False,
    }


_BUILDERS = {
    'get_metric_values': get_metric_values_schema,
    'compare_periods': compare_periods_schema,
    'compare_product_set': compare_product_set_schema,
    'filter_products': filter_products_schema,
}

# 模型可见业务描述（一句话；注册工具时直接用，调整时与合同文档同步）
TOOL_DESCRIPTIONS = {
    'get_metric_values':
        '读取一个业务范围（全池 / 一个或多个目录品类 / 指定商品）在某个单期（目录周期、日期窗口'
        '或相对时间）的指标原始读数（M1–M5）；可对本次范围做 M1/M2 合计，并可 group_by=category '
        '按品类分组给合法合计与成员贡献比例。品类成员由服务端从可信目录解析，不需要模型枚举。'
        '数值由数据服务计算，缺失返回 unknown 原因，不代 0。',
    'compare_periods':
        '比较同一业务范围在基期与目标期（目录周期对、两个日期窗口或相对时间对）的变化：'
        '可在一次请求里比较多个指标（metrics[]），逐商品给出两期值、绝对差、百分比差与涨跌'
        '方向；范围由参数 scope 给（全池 / 一个或多个目录品类 / 显式商品），品类成员由服务端'
        '从可信目录解析。缺失/零分母/不可比按指标各自返回状态码，不代 0，不同指标之间不合并'
        '判断。',
    'compare_product_set':
        '在同一单期内比较一个业务范围内一组商品的同一指标：kind=rank 给集合内排名，'
        'kind=median 给集合中位数基准（高于/持平/低于）；范围由参数 scope 给（全池 / 一个或'
        '多个目录品类 / 显式商品），品类成员由服务端从可信目录解析；无可用值的成员单列缺失组，'
        '不参与比较。',
    'filter_products':
        '在一个业务范围（全池 / 一个或多个目录品类 / 指定商品）内按 1–4 条数值条件（AND）'
        '筛出完整成员名单：条件可针对单期取值（field=value）或跨期变化（field=absolute_diff / '
        'percent_diff）。返回命中、不命中与无法判定三组、实际分母与已确认命中占比；'
        '缺数按无法判定单列，不代 0。阈值必须由你按本次问题给出——本工具不内置「值得加量」'
        '「明显下滑」之类经营默认阈值。',
}


def build_input_schemas():
    """返回四个 Tool 的完整 `inputSchema`（深拷贝展开，互不共享可变对象）。"""
    return {name: copy.deepcopy(builder()) for name, builder in _BUILDERS.items()}


# check_arg_types 专用只读快照：每次工具调用会经本函数 2 次（调用前钩子+请求构造各一次，
# 输出钩子再来一轮），逐次深拷贝全量 Schema 纯属浪费。快照只被本函数读取、绝不改写；
# 公共 `build_input_schemas` / `tool_specs` 保持每次深拷贝的「互不共享可变对象」合同不变。
_SCHEMA_PROPS_CACHE: dict | None = None


def _schema_props(tool: str) -> dict:
    global _SCHEMA_PROPS_CACHE
    if _SCHEMA_PROPS_CACHE is None:
        _SCHEMA_PROPS_CACHE = {name: schema['properties']
                               for name, schema in build_input_schemas().items()}
    return _SCHEMA_PROPS_CACHE[tool]


def tool_specs():
    """返回 MCP 工具的描述与 inputSchema，作为接口定义的唯一来源。"""
    schemas = build_input_schemas()
    return {name: {'description': TOOL_DESCRIPTIONS[name], 'inputSchema': schemas[name]}
            for name in _BUILDERS}


def check_arg_types(tool, args):
    """适配层类型检查：只查 `to_request()` 会做 `list(...)` 的**数组**参数。

    为什么必须有这一层：`to_request` 对 `metrics` / `product_ids` 执行数组展开（`list(...)`），
    误传字符串会被拆成字符列表、误传对象会变成键列表——非法输入被悄悄改写成另一种请求，
    极端情况下还可能跑出一次「成功」查询；`scope` 及其内层数组走 `copy.deepcopy`，不会被
    拆分，但形态仍要与合同一致。MCP Client
    不替 Server 强制完整 Schema，所以这条防线只能在 Server 侧（本函数）。

    为什么只查数组**形态**、不预判取值：`period_id` / `window` / `time` / `metric` / `kind` /
    `aggregate` / `group_by` 的值原样搬运，类型/取值错误由数据层按其 op 键闭集拒绝，本函数不预判业务语义、不新增第二套取值校验（键闭集拒绝另见函数末尾，只对
    **非筛选类**三工具生效）。

    （`filter_products`）：`conditions` 也受同一条**数组结构**保护（该工具 Schema 把它
    声明为数组，`to_request` 对它做 `copy.deepcopy` 而非 `list(...)` 展开——误传字符串
    不会被拆成字符列表，但结构仍要与合同一致）。该工具的**未知业务键**不在本层拒绝——
    键闭集与时间形态由数据层裁决（`unknown_keys` /
    `time_semantics_invalid`），适配层先拒会把业务拒绝改写成适配层错误。

    期望类型取自该工具 `inputSchema.properties` 的 `type` / `items.type`（同一份合同的
    即时投影，不另写 Schema）；另对 `scope` 的内部数组做同样保护（仍是同一份 Schema 的
    嵌套投影）。`product_ids` 自 不再是 `get_metric_values` 的 Schema 属性，但数据层
    保留为兼容别名——直接调用方误传字符串时仍须拒绝，不得被拆成字符列表。返回人类可读的
    错误列表；空列表＝通过。
    """
    if tool not in TOOL_OPS:
        raise ValueError('未知 MCP 工具: %r' % (tool,))
    props = _schema_props(tool)
    errors = []

    def _check_array(key, value, items):
        if not isinstance(value, list):
            errors.append('%s 必须是数组，收到 %s' % (key, type(value).__name__))
            return
        if (items or {}).get('type') == 'string':
            for index, item in enumerate(value):
                if not isinstance(item, str):
                    errors.append('%s 的第 %d 项必须是字符串，收到 %s'
                                  % (key, index, type(item).__name__))

    for key, prop in props.items():
        if key not in args:
            continue
        if prop.get('type') == 'array':
            _check_array(key, args[key], prop.get('items'))
        elif key == 'scope' and isinstance(args[key], dict):
            inner = prop.get('properties') or {}
            for sub in ('product_ids', 'categories'):
                if sub in args[key]:
                    _check_array('scope.%s' % sub, args[key][sub],
                                 (inner.get(sub) or {}).get('items'))
    # 数据层兼容别名：不在 MCP Schema 里，但 `to_request` 仍会 list()——同样先做数组保护。
    if 'product_ids' in args and 'product_ids' not in props:
        _check_array('product_ids', args['product_ids'], {'type': 'string'})
    # filter_products 的未知键和条件取值交由数据层判定，适配层只检查数组等结构。
    # 其余查询工具在 Server 侧拒绝未知业务键，避免拼写错误被丢弃后静默改变查询含义。
    # 允许键来自工具 schema，加上数据层支持的 product_ids；参数中的 run_id 丢弃，身份由会话绑定。
    if tool != 'filter_products':
        allowed_keys = set(props) | {'product_ids', 'run_id'}
        unknown = sorted(set(args) - allowed_keys)
        if unknown:
            errors.append('%s 未知参数：%s（可用 %s）'
                          % (tool, ','.join(unknown), '/'.join(sorted(allowed_keys))))
    return errors


def check_time_groups(tool, args):
    """适配层时间键必给检查：`compare_periods` 时间键一个都没给时拒绝。

    为什么只查「全缺」这一种形态：数据层对 diff 在不显式传周期对时会回退默认
    p1→p2（命名常量语义，供内部直连调用方使用），经 MCP 工具面漏给时间会拿到
    模型未请求周期对的比较结果——静默成功，不是业务拒绝，与 Schema oneOf
    「恰给一组」的承诺不符。半对/混用/越界仍由数据层按其错误码权威拒绝，
    本函数不预判业务语义（与 check_arg_types 同一条纪律：适配层只拦「会被
    静默改写」的输入，不复刻业务校验）。返回人类可读错误列表；空列表＝通过。
    """
    if tool != 'compare_periods':
        return []
    if any(key in args for key in _ALL_TIME_KEYS):
        return []
    return ['compare_periods 需给出一种时间参数（%s），不得省略'
            % ' / '.join('+'.join(group) for group in PAIR_TIME_GROUPS)]


def to_request(tool, args):
    """工具参数 → 数据层 `request`（固定 op 映射；只搬运，不补默认值、不改写时间）。

    多余键不做裁剪也不做映射：合同由 `inputSchema`（`additionalProperties:false`）与数据层
    逐 op 键闭集两层保证，本函数只负责把合法参数按数据层字段名排好。

    数组参数先过 `check_arg_types`：类型不对就**拒绝构造**（`ValueError`），绝不把
    「字符串被拆成字符列表」这类改写后的请求交给数据层。Server 侧会先调
    `check_arg_types` 并返回适配层参数错误，这里再兜一道，保证任何直接调用方同样安全。
    """
    if tool not in TOOL_OPS:
        raise ValueError('未知 MCP 工具: %r' % (tool,))
    type_errors = check_arg_types(tool, args)
    if type_errors:
        raise ValueError('参数类型不合法（%s）：%s' % (tool, '；'.join(type_errors)))
    op = TOOL_OPS[tool]
    if tool == 'get_metric_values':
        req = {'op': op}
        if 'scope' in args:
            req['scope'] = copy.deepcopy(args['scope'])
            if 'product_ids' in args:
                # 两种范围写法同时出现：不猜优先级，原样交给数据层按 拒绝
                # （`scope_conflict`）。适配层不得静默丢掉其中一个、把非法请求改写成合法请求。
                req['product_ids'] = list(args['product_ids'])
        elif 'product_ids' in args:
            # 数据层兼容别名：不出现在 MCP Schema，但直接调用方仍可用；与 scope 互斥。
            req['product_ids'] = list(args['product_ids'])
        else:
            raise ValueError('get_metric_values 缺业务范围：需要 scope（或兼容别名 product_ids）')
        req['metrics'] = list(args['metrics'])
        if 'group_by' in args:
            req['group_by'] = copy.deepcopy(args['group_by'])
    elif tool == 'filter_products':
        # 筛选面**不预判业务合法性**——键闭集（`unknown_keys`）、条件闭集
        # （`unknown_metric` / `unknown_operator` / `invalid_threshold` / `field_period_mode_mismatch`）
        # 与时间形态（`time_semantics_invalid`）全部由数据层按 `COHORT_REQUEST_KEYS` 裁决
        # （硬边界：数据层是业务拒绝的唯一权威）。因此这里原样搬运全部业务键，
        # 只丢两个非业务键：`run_id`（模型不可更换根任务）与 `op`（适配层固定映射键——
        # Schema 的 additionalProperties:false 本就不含它，直接调用方误传时按固定映射丢弃，
        # 不静默采信调用方传入的 op 值）。
        req = {k: copy.deepcopy(v) for k, v in args.items() if k not in ('run_id', 'op')}
        req['op'] = op
        req['scope'] = copy.deepcopy(args['scope'])
        req['conditions'] = copy.deepcopy(args['conditions'])
    else:
        req = {'op': op}
        # scope 与顶层 product_ids 互斥；模型 schema 只公开 scope。
        if 'scope' in args:
            req['scope'] = copy.deepcopy(args['scope'])
            if 'product_ids' in args:
                req['product_ids'] = list(args['product_ids'])
        elif 'product_ids' in args:
            req['product_ids'] = list(args['product_ids'])
        else:
            raise ValueError('%s 缺业务范围：需要 scope（或兼容别名 product_ids）' % tool)
        if tool == 'compare_periods':
            # metrics 与 metric 互斥；冲突或缺失交由数据层明确拒绝。
            if 'metrics' in args:
                req['metrics'] = list(args['metrics'])
            if 'metric' in args:
                req['metric'] = args['metric']
        else:
            req['metric'] = args['metric']
            req['kind'] = args['kind']
    for key in ('period_id', 'window', 'time', 'base_period_id', 'target_period_id',
                'base_window', 'target_window'):
        if key in args:
            req[key] = copy.deepcopy(args[key])
    if 'aggregate' in args:
        req['aggregate'] = args['aggregate']
    return req


__all__ = ['TOOL_OPS', 'TOOL_DESCRIPTIONS', 'METRIC_ENUM', 'SUM_METRIC_ENUM',
           'SUM_AGGREGATE_VALUES', 'AGGREGATE_MIN_PRODUCTS', 'SLOT_FORM_KEYS',
           'SINGLE_TIME_GROUPS', 'PAIR_TIME_GROUPS', 'time_slot', 'time_single', 'time_pair',
           'time_slot_period_only', 'time_filter', 'FILTER_TIME_GROUPS',
           'COHORT_CONDITION_KEYS', 'COHORT_OPERATORS', 'COHORT_MAX_CONDITIONS', 'COHORT_FIELDS',
           'scope_schema', 'get_metric_values_schema', 'compare_periods_schema',
           'compare_product_set_schema', 'filter_products_schema',
           'build_input_schemas', 'tool_specs', 'check_arg_types',
           'to_request']
