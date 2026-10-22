# -*- coding: utf-8 -*-
"""经营 Agent 的共享业务说明、工具能力描述与入口投影。

仅生成确定性文本与只读目录投影，无模型或网络调用。
shared_business_note 说明指标、集合基准、可比性与调查边界；
oagent_period_brief 描述查询能力与请求形状，不安排查询路线或提供答案。
商品身份复用 oi03_brief 与 period_view_generator，时间约束来自原生输入。"""
import hashlib
import json
import os
import sys

from . import oi03_brief as OB                    # noqa: E402
from . import period_view_generator as PVG        # noqa: E402
from . import period_resolver as PR               # noqa: E402  （时间语义版本串单点在其内）
from . import operating_catalog as OC             # noqa: E402  （日级覆盖/业务日锚点单点；零 import）
from . import qctx_period as QP                    # noqa: E402  （请求键闭集单点，供能力说明取键）

OAGENT_SHARED_NOTE_VERSION = 'oagent-shared-note-1.1'
OAGENT_PERIOD_BRIEF_VERSION = 'oagent-period-brief-1.1'
OAGENT_PERIOD_BRIEF_CONTRACT_VERSION = 'oagent-period-brief-contract-1.0'
#  相对时间语义 schema 版本（单一事实源=period_resolver.PERIOD_TIME_SEMANTICS_VERSION；
# 本模块只重导出给模型可见合同文本与版本核对使用）。
OAGENT_TIME_SEMANTICS_VERSION = PR.PERIOD_TIME_SEMANTICS_VERSION
OAGENT_ENTRY_CONTRACT_VERSION = 'operating_agent_entry_v2'
OAGENT_ROOT_BUDGET = {'period_query_attempts': 6, 'evidence_calls': 2, 'specialist_calls': 2}


def oagent_period_brief_contract(legacy_compat=False):
    """周期查询请求形状、操作能力与语义合同。"""
    metrics = '；'.join('%s %s（%s，%s）' % (k, v['name'], v['unit'], v['role'])
                       for k, v in (('M1', {'name': '订单量', 'unit': '单', 'role': '结果'}),
                                    ('M2', {'name': '播放量', 'unit': '次', 'role': '过程'}),
                                    ('M3', {'name': '点击率', 'unit': '比率（条均，非曝光加权）', 'role': '过程'}),
                                    ('M4', {'name': '千次播放订单 OPM', 'unit': '单/千次播放', 'role': '结果'}),
                                    ('M5', {'name': '千次播放成交额 GPM', 'unit': '元/千次播放（VV 加权）', 'role': '结果'})))
    if legacy_compat:
        operation_contract = (
            'diff（同对象同指标 target−base 与百分比差，需 base_period_id/target_period_id）；'
            'set_compare（单周期内同集合排名或中位数基准，kind=median|rank）；'
            'states（对象身份/状态/属性/记录状态，跨期操作）')
        states_request = 'states 用 {"op":"states","product_ids":[...]}。'
        period_scope = (
            '；diff 未显式给 base/target 时若本轮只允许一个周期=拒绝；states 返回视图两期'
            '之间的状态/属性变化（无周期参数可收窄），仅当本轮同时允许两个周期时可用。')
    else:
        operation_contract = (
            'diff（同对象同指标 target−base 与百分比差；'
            'absolute_diff 可用时附 direction=up/down/flat）；period_diff_set.summary 含 unique_product_count、'
            'direction_counts{up_count/down_count/flat_count}、absolute_diff_comparable_count、'
            'absolute_diff_unavailable_by_state、percent_diff_available_count、percent_diff_unavailable_by_state；'
            'summary按唯一 product_id 计数，方向只按 numeric absolute_diff 汇总；绝对差和百分比可用性分开统计，'
            'base=0 时绝对差和方向仍可用，但百分比为 unknown(zero_denominator)）；'
            'set_compare（单周期内同集合排名或中位数基准，kind=median|rank）；'
            'states（对象身份/状态/属性/记录状态，可选显式周期对）')
        states_request = (
            'states 可用 {"op":"states","product_ids":[...],"base_period_id":"...",'
            '"target_period_id":"..."}（周期对须同时提供；也可同时省略）。')
        period_scope = (
            '；diff 未显式给 base/target 时若本轮只允许一个周期=拒绝；states 显式给周期对时'
            '只比较该两期且两期都须获准；省略周期对时保持首末周期旧行为，仅当本轮允许完整视图周期集时可用。'
            'states 每对象均回显 comparison_window（base_period_id/target_period_id/state=ok|period_missing|record_conflict），'
            '无变化也回显窗口；周期对须同时提供且周期 ID 必须有效，缺周期返回 period_missing，非法请求结构化拒绝，'
            '不回退到首末周期。')
    return (
        'pctx_v1 周期查询能力说明（%s；数据面——周期目录、商品目录与对象池——见任务输入，'
        '不在此重复）：\n'
        '[1] 周期是固定模拟周期，不提供真实同比/环比、任意日期滚动或连续趋势。\n'
        '[2] 指标：%s。\n'
        '[3] 操作：value（对象集合×周期×指标取值）；%s。\n'
        '[4] 请求形状：{"op":"value","product_ids":[...],"period_id":"...","metrics":["M1"]}；'
        'diff 用 {"op":"diff","product_ids":[...],"metric":"M1","base_period_id":"...",'
        '"target_period_id":"..."}；set_compare 用 {"op":"set_compare","product_ids":[...],'
        '"period_id":"...","metric":"M4","kind":"median"}；%s\n'
        '    允许的键只有 %s；出现其它键=未知键，一律确定性拒绝。\n'
        '[5] 缺失与零分母：unknown 一律附原因码（zero_denominator / period_missing / field_missing / '
        'window_incomparable），不静默代 0；不返回 *_level / round_verdict / 动作建议。\n'
        '[6] 周期约束：查询的周期（period_id/base_period_id/target_period_id）只能取自本轮任务输入'
        '的允许周期%s'
        % (OAGENT_PERIOD_BRIEF_CONTRACT_VERSION, metrics, operation_contract, states_request,
           '、'.join(OB.PERIOD_QUERY_KEYS), period_scope))


def oagent_time_semantics_brief():
    """相对时间语义说明（current 模型可见合同的单点文本，供 current 面直取/核对）。"""
    return (
        '相对时间语义（time 字段；%s；服务端确定性解析，解析结果随响应 resolved_window 回显）：\n'
        '[T1] 口语相对时间（"上一期""上上期""最近一期""昨天""上周同一天""本月以来"等）不要自行换算成 '
        'canonical period_id 或具体日期：在 query 里加 time 字段表达结构化语义，服务端按本轮允许周期'
        '（周期形态）或日级覆盖 %s~%s（日期形态）确定性解析（时间序以目录/覆盖真实日期为准，不以 ID '
        '字符串判断），再把 canonical period 身份或真实日期窗口注入查询；解析出的窗口以响应 '
        'resolved_window 为准（含原始语义、解析出的 base/target 或真实起止日期、目录时间序、方向与'
        '范围校验结果），可直接核对。\n'
        '[T2] 形状：单窗口操作（value/set_compare/daily_series）用 {"time":{"period":S}}；'
        '双窗口操作（diff/states）用 {"time":{"base":S,"target":S}}（base=较早，target=较晚）。'
        'S 有两种形态，同一槽位只给一种：周期形态={"which":"latest"|"previous"|"two_ago"} 或 '
        '{"offset":整数≤0}（0=最新期，-1=上一期，-N=回溯 N 期；回溯编号越大越早）；日期形态=%s '
        '恰给一种——days_ago（以模拟业务日 %s 回推的单日：1=昨天 %s、2=前天）、weeks_ago（以最新'
        '完整日 %s 回推整周的同一天：1=上周同一天）、date（显式单日 YYYY-MM-DD）、date_range'
        '（{start_date,end_date}，含首末）、last_days（最近 N 天，末日=最新完整日）、month'
        '（完整自然月 this/last/YYYY-MM，当月未完结只取已覆盖部分）、month_to_date（月内同进度：'
        'this/last/YYYY-MM，与最新完整日同「日」对齐；问「本月以来比上月同期」用它）。\n'
        '[T3] 周期形态锚点=本轮允许周期中目录日期最晚的一期，解析只落在本轮允许周期内。结构化拒绝码：'
        'time_semantics_invalid（结构非法/形状与操作不匹配/offset 非法/一槽混用两形态/time 与显式'
        '周期键或显式窗口键混用）、time_out_of_allowed_scope（回溯落出允许周期；日期形态落出日级'
        '覆盖）、time_window_direction_invalid（base 不早于 target）、period_mode_conflict（同轮'
        '混用不同形态）。执行前确定性拒绝不消耗查询额度，实际查询返回 unknown/no-data 仍计数；'
        '需要更早期时由入口扩允许范围，不从题面扩展。\n'
        '[T4] 显式 canonical period 请求继续按现行合同工作（周期须 ∈ 允许周期；显式周期对同样'
        '校验方向：base 必须早于 target，反向=结构化拒绝）；纯单期查询不受影响。\n'
        '[T5] 日期形态只对 get_metric_values / compare_periods / compare_product_set / '
        'daily_series 开放（states 与筛选工具 filter_products 的时间槽位'
        '仍只用周期形态）；日期窗口与旧周期身份是两套写法，同一次请求不混用。完整自然月之间的'
        '总量可直接比（窗口不等长按各窗口整体口径给差，报告时写明两段真实起止日期与天数）；'
        '完整月与未完结月、等长与不等长窗口不是**同进度**环比，前后两段真实起止日期先看清，'
        '不把未完结月当作完整月环比来表述。要连续多日走势用 daily_series 一次取回序列，不逐日重复查数。'
        % (OAGENT_TIME_SEMANTICS_VERSION, OC.DATA_COVERAGE_START, OC.DATA_COVERAGE_END,
           '/'.join(PR.DATE_FORM_KEYS), OC.DEMO_BUSINESS_DATE, OC.LATEST_COMPLETE_DATE,
           OC.LATEST_COMPLETE_DATE))


def shared_business_note():
    """生成共享经营口径与调查完成标准，随任务输入交付，不安排查询路线或提供答案数值。"""
    return (
        '共享经营口径（%s；对主 Agent 与专项调查助手同样生效）：\n'
        '1. 对象分层：分析对象（本次要回答的对象）、显式比较对象（用户选出的对照）、'
        'benchmark 集合（可查询/排名的目录池）三者分开。默认 benchmark 集合=目录内全部 %d 个'
        '商品；目录池不代表全市场；若说「同品类」，须按实际类目筛选（如西式快餐），不假装'
        '全池是同品类。用户明确的范围限制优先，冲突会影响结论时才澄清。\n'
        '2. 商品与周期由入口选择提供，问题表达经营目的；不要求用户重输完整商品名、内部 ID 或'
        'JSON。\n'
        '3. 查询只使用同一版本的 %d 商品日级模拟事实（覆盖 %s~%s；目录周期、完整自然月与月内'
        '同进度都是这批日级事实的聚合）；旧 26 案例 fixture 不作周期证据。\n'
        '4. 指标口径：M1=商品周期订单量；M2=该商品该周期所有视频播放量之和（不做合格过滤）；'
        'M3=所有非缺失 plc_ctr 行的条均（分母=非缺失行数，不是曝光加权，不是合格子集）；'
        'M4=合格同视频配对的 Σpaid_orders/Σvv×1000（单/千次播放）；M5=合格配对的 '
        'Σ(gpm×vv)/Σvv（元/千次播放，VV 加权）。\n'
        '5. M1、M2、M4 的参与群体不同，不能用 M1=M2×M4/1000 反推分解总订单贡献；机制因果'
        '需要额外证据，当前没有就明确说没有，不把相关变化写成因果。\n'
        '6. 样本数量或构成变化需要解释其影响；不能仅凭合格行数变化就自动宣布所有指标不可比。'
        '分别说明指标定义、窗口及覆盖变化。\n'
        '7. 「卖得怎么样」默认核对 M1、M2 的两期值及变化（含绝对差与百分比差，并说明两期是否'
        '可比）；同集合水平默认=该集合本周期该指标的中位数（高于/低于/持平）；排名按实际指标值。\n'
        '8. published 表示当期是否新增视频发布，无新增仍可有旧视频流量；影响当前判断的发布状态'
        '必须解释，无关状态不机械影响任务完成度。\n'
        '9. 调查至少交付已查清的经营环节、依据与条件化处理建议；可查部分未做完时，不能用'
        '「后续调查清单」代替完成。总体池也下降不等于个体机制已证明；个体跌幅更大只能支持'
        '「共同下行不足以充分解释差异」等有限判断，不升格为因果。\n'
        '数据为 Demo 模拟口径：非真实平台日期、非线上效果、非生产数据。'
        % (OAGENT_SHARED_NOTE_VERSION, len(all_product_ids()), len(all_product_ids()),
           OC.DATA_COVERAGE_START, OC.DATA_COVERAGE_END))


def oagent_period_brief():
    """生成周期查询能力说明。

对象可来自完整目录，周期服从入口的明确时间约束，状态查询默认不扩大到其它周期。"""
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
        '[1] 粒度与窗口：日级事实为最细粒度（覆盖 %s~%s，模拟业务日 %s，「昨天」=%s）；'
        '目录周期与完整自然月、月内同进度都是同一批日级事实的聚合（比率类指标按各自分子/分母'
        '重算，不复用周比率均值）。相对日期用 time 字段表达（%s 恰给一种），服务端解析成真实'
        '起止日期并在响应回写；不提供真实同比或跨年对比。周期目录：%s。\n'
        '[2] 对象：目录内全部 %d 个商品均可查询（可读名与选择标签一致，product_id 见目录）。'
        'snapshot_id/case_id 属**旧命名空间**，与本周期命名空间互斥，混用=namespace_conflict。\n'
        '[3] 指标：%s。\n'
        '[4] 操作：value（对象集合×周期或日期窗口×指标取值，可多指标；'
        'aggregate=sum 仅支持 M1/M2，M3/M4/M5 不支持跨商品直接求和——需读其数值用不带 '
        'aggregate 的 value，需集合排名或中位数比较用 set_compare）；'
        'diff（同对象同指标 target−base 与百分比差；'
        '绝对差可用时附 direction=up/down/flat）；period_diff_set.summary '
        '含 unique_product_count、direction_counts{up_count/down_count/flat_count}、absolute_diff_comparable_count、'
        'absolute_diff_unavailable_by_state、percent_diff_available_count、percent_diff_unavailable_by_state；'
        'M1/M2 的多商品 diff 另附 summary.category_totals——按目录品类给出本次选中可比商品的两期合计、'
        '绝对变化与百分比变化（跨期品类合计及变化用一次 diff 即可，不必对同一批商品重复发两次 value）；'
        'summary按唯一 product_id 计数，方向只按 numeric absolute_diff 汇总；绝对差和百分比可用性分开统计，'
        'base=0 时绝对差和方向仍可用，但百分比为 unknown(zero_denominator)；'
        'set_compare（单周期内同集合排名或中位数基准，kind=median|rank）；'
        'daily_series（同一对象×指标在日期窗口内逐日取值，单次取回最近几天的连续读数，'
        '不必逐日重复查）；states（对象身份/状态/属性/记录状态，可选显式周期对）。\n'
        '[5] 请求形状：{"op":"value","product_ids":[...],"period_id":"...","metrics":["M1"]}；'
        '日期窗口用 {"op":"value","product_ids":[...],"metrics":["M1"],'
        '"window":{"start_date":"...","end_date":"..."}} 或 time 字段表达相对日期；'
        'diff 用 {"op":"diff","product_ids":[...],"metric":"M1","base_period_id":"...",'
        '"target_period_id":"..."}（日期窗口比较用 base_window/target_window 成对给）；'
        'set_compare 用 {"op":"set_compare","product_ids":[...],'
        '"period_id":"...","metric":"M4","kind":"median"}；daily_series 用 '
        '{"op":"daily_series","product_ids":[...],"metrics":["M1"],"window":{...}}；'
        'states 可用 {"op":"states","product_ids":[...],'
        '"base_period_id":"...","target_period_id":"..."}（周期对须同时提供；也可同时省略）。\n'
        '    允许的键只有 %s（相对时间另用 time 字段，由时间语义解析器剥离）；出现其它键=未知键，'
        '一律确定性拒绝。\n'
        '[6] 缺失与零分母：unknown 一律附原因码（zero_denominator / period_missing / field_missing / '
        'window_incomparable），不静默代 0；不返回 *_level / round_verdict / 动作建议。\n'
        '[7] 周期与窗口约束：查询的周期（period_id/base_period_id/target_period_id）只能取自本轮'
        '入口允许周期；显式日期窗口（window/base_window/target_window）按日级覆盖 %s~%s 校验，'
        '越界=time_out_of_allowed_scope；diff 未显式给 base/target 时若本轮只允许一个周期=拒绝；'
        '日期窗口比较两侧须成对给（base_window/target_window），base 早于 target；窗口不等长仍按'
        '各窗口整体口径给差（响应回 window_aligned=false 与实际天数 window_length_days），'
        '完整自然月之间的总量可直接比、报告时写明两段真实起止日期与天数；完整月与未完结月、'
        '等长与不等长窗口不是**同进度**环比，不能把未完结月当作完整月环比来表述。'
        'states 显式给周期对时'
        '只比较该两期且两期都须获准；省略周期对时保持首末周期旧行为，仅当本轮允许完整视图周期集时可用。'
        'states 每对象均回显 comparison_window（base_period_id/target_period_id/state=ok|period_missing|record_conflict），'
        '无变化也回显窗口；周期对须同时提供且周期 ID 必须有效，缺周期返回 period_missing，非法请求结构化拒绝，'
        '不回退到首末周期。'
        % (OAGENT_PERIOD_BRIEF_VERSION, OC.DATA_COVERAGE_START, OC.DATA_COVERAGE_END,
           OC.DEMO_BUSINESS_DATE, OC.LATEST_COMPLETE_DATE, '/'.join(PR.DATE_FORM_KEYS),
           periods, len(PVG.PRODUCTS), metrics,
           '、'.join(QP.QUERY_REQUEST_KEYS), OC.DATA_COVERAGE_START, OC.DATA_COVERAGE_END))


def entry_options():
    """复用商品目录的入口选项与范围选项。"""
    return OB.entry_options()


def label_to_product_id(label):
    """复用目录标签到商品 ID 的确定性映射。"""
    return OB.label_to_product_id(label)


def all_product_ids():
    return OB.all_product_ids()


def entry_catalog():
    """返回当前商品身份表与周期目录。"""
    return OB.entry_catalog()


def root_budget():
    """根任务初始执行上限"""
    return dict(OAGENT_ROOT_BUDGET)


def ctx_versions():
    """计算入口上下文的合同版本与内容指纹，供初始化请求核对。

    客户端回传取上下文时的 expected_versions；版本或内容发生变化时拒绝初始化。"""
    catalog = entry_catalog()
    return {
        'entry_contract': OAGENT_ENTRY_CONTRACT_VERSION,
        'shared_business_note': OAGENT_SHARED_NOTE_VERSION,
        'period_brief': OAGENT_PERIOD_BRIEF_VERSION,
        'view_version': catalog['view_version'],
        'catalog_sha256': _hash_text(json.dumps(catalog, ensure_ascii=False, sort_keys=True))[:16],
        'shared_note_sha256': _hash_text(shared_business_note())[:16],
    }


def agent_tool_specs():
    """生成服务端接口的能力描述与预算说明。"""
    return [
        {'name': 'oagent_query',
         'description': '周期只读查询（pctx_v1，Demo 模拟视图，对象池见任务输入）：参数 query 为请求对象，'
                        '形状见上方能力说明；允许键之外一律确定性拒绝。每次尝试（含被拒与重复）'
                        '消耗根任务查询额度，当前 run 最多 %d 次尝试。'
                        % OAGENT_ROOT_BUDGET['period_query_attempts']},
        {'name': 'oagent_evidence',
         'description': '单周期同源取证（只读）：对单个商品单个周期做身份/状态/窗口一致校验，返回 '
                        'pev 证据 id、该对象该周期已核查指标值（缺失/零分母按 unknown+原因码，不代 0）'
                        '与状态披露。只覆盖身份、状态与单期指标核对，不提供素材、渠道等机制证据。'
                        '当前 run 最多 %d 次调用。' % OAGENT_ROOT_BUDGET['evidence_calls']},
        {'name': 'oagent_specialist',
         'description': '专项经营调查（委派 Qwen 专项调查助手，只读）：只负责**可独立交付的'
                        '跨对象 benchmark 扫描**——跨多个商品、多个指标或多个周期寻找共同趋势、'
                        '异常对象与相对位置，返回 1–3 条有证据支持的局部发现；不负责目标商品'
                        '深挖、最终经营判断与建议（那些由你完成）。简单取值由你直接查询，'
                        '不强制委派。参数 specialist_request 为 JSON 对象：{sub_question, '
                        'target_objects, benchmark_scope?, periods, evidence_refs?, '
                        'expected_delivery, user_question?}；周期须 ∈ 允许周期。'
                        '当前 run 最多 %d 次委派；专项内查询与你共享同一根任务查询/取证额度'
                        '（含被拒与重复）。返回 findings/uncertainties/completion 结构化结果。'
                        % OAGENT_ROOT_BUDGET['specialist_calls']},
    ]


# 相对时间结构由工具 schema 定义，解析后的窗口在查询响应中回显。
OAGENT_QUERY_TIME_TOOL_CLAUSE = (
    '相对时间不必自行换算成 canonical period_id：query 使用本工具参数 schema 定义的 time 字段表达；'
    '服务端按本轮允许周期或日级覆盖确定性解析成真实周期或真实日期窗口，并在响应回显 resolved_window。')


def current_agent_tool_specs():
    """返回 oagent_query 的当前模型可见描述；MCP 查询工具与知识工具分别维护其描述。"""
    base = {t['name']: t for t in agent_tool_specs()}
    query_description = base['oagent_query']['description'].replace(
        '形状见上方能力说明；允许键之外一律确定性拒绝。',
        '字段与按操作必填关系由参数 schema 约束；无效请求由服务端结构化拒绝。')
    query_description = query_description.replace(
        '每次尝试（含被拒与重复）消耗根任务查询额度',
        '每次实际执行的数据查询消耗根任务查询额度；执行前拒绝与重复缓存命中不消耗额度')
    query_description = query_description.replace(
        '当前 run 最多 %d 次尝试。' % OAGENT_ROOT_BUDGET['period_query_attempts'],
        '当前 run 最多 %d 次实际查询。' % OAGENT_ROOT_BUDGET['period_query_attempts'])
    return [
        {'name': 'oagent_query',
         'description': query_description
             + ' query 只支持 states/daily_series'
             '（单期取值、跨期比较、集合排名/中位数与条件筛选分别由 get_metric_values / '
             'compare_periods / compare_product_set / filter_products 四个查询工具提供，'
             '本工具不再提供这四个 op；catalog 也不再是模型可选操作——可信目录只用于内部'
             '范围解析，商品目录随任务输入下发；'
             'states=跨期状态/属性变化核查（发布、价格、属性漂移）或用户明确要求的身份/一致性核对，'
             '不用于复核 compare_periods 已判定的正常可比性；'
             'daily_series=同一对象×指标在日期窗口内逐日取值，单次取回连续多日读数，'
             '不必逐日重复查）；'
             '确定性拒绝返回 error_code 与 detail。'
             + OAGENT_QUERY_TIME_TOOL_CLAUSE},
    ]


def _hash_text(s):
    return hashlib.sha256(str(s).encode('utf-8')).hexdigest()


def brief_hash():
    return {'shared_business_note': _hash_text(shared_business_note()),
            'period_brief': _hash_text(oagent_period_brief()),
            'versions': {'shared_business_note': OAGENT_SHARED_NOTE_VERSION,
                         'period_brief': OAGENT_PERIOD_BRIEF_VERSION,
                         'entry_contract': OAGENT_ENTRY_CONTRACT_VERSION}}


def brief_json():
    return json.dumps({'shared_business_note': shared_business_note(),
                       'period_brief': oagent_period_brief(),
                       'entry_options': entry_options(),
                       'catalog': entry_catalog(),
                       'root_budget': root_budget(),
                       'versions': brief_hash()['versions']}, ensure_ascii=False, sort_keys=True)


__all__ = ['OAGENT_SHARED_NOTE_VERSION', 'OAGENT_PERIOD_BRIEF_VERSION',
           'OAGENT_PERIOD_BRIEF_CONTRACT_VERSION', 'OAGENT_TIME_SEMANTICS_VERSION',
           'OAGENT_ENTRY_CONTRACT_VERSION', 'OAGENT_ROOT_BUDGET',
           'shared_business_note', 'oagent_period_brief', 'oagent_period_brief_contract',
           'oagent_time_semantics_brief', 'current_agent_tool_specs',
           'OAGENT_QUERY_TIME_TOOL_CLAUSE',
           'entry_options', 'label_to_product_id', 'all_product_ids', 'entry_catalog',
           'root_budget', 'ctx_versions', 'agent_tool_specs', 'brief_hash', 'brief_json']
