# -*- coding: utf-8 -*-
"""解析终答展示意图，核验引用、回查原始值并装配 display_blocks。

业务正文与展示意图分别交付；解析失败不泄漏损坏 JSON。
LiveEvidenceSource 从当前 run 的服务端账本回查，来源不可用时不交付展示块。
展示块独立核验签发记录、运行归属、对象/指标/周期与封闭集合计算；
自然语言公式不执行。引用与计算问题形成局部 data_issues，口径提醒留在内部核验面。
Wire 键面由 core.wire_contract.display_blocks_wire 维护。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from statistics import median

from core.data.operating_catalog import PRODUCTS
from core.data.period_view_generator import period_by_id
from core.data.qctx_period import category_totals_from_value_pairs
from core.wire_contract import display_blocks_wire, unknown_reason_tokens

log = logging.getLogger('agents_oagent.display_intent')

# ---------- 0. 常量 ----------

MARKER_START = '<<<DISPLAY_INTENT>>>'
MARKER_END = '<<<END_DISPLAY_INTENT>>>'

STATUS_EXACT = 'exact'        # 双标记齐全且 JSON 合法。
STATUS_SALVAGED = 'salvaged'  # 缺结束标记，但平衡扫描解析出唯一完整 JSON 对象
STATUS_FAILED = 'failed'      # 不可靠解析：降级标记区安全剥离后的正文

# 内部核验结果使用独立 schema；引用或计算问题记录为块级 data_issues。
RESULT_SCHEMA = 'uix06_phase2.display_result/0.6'

# post_process 诊断槽位新增值：终答剥掉标记区后无任何业务正文（只给了意图区）。
BRANCH_INTENT_ONLY = 'intent_only_answer'

# 证据回查使用本次运行的 ToolServerClient，不从离线保存结果补充证据。
CHANNEL_SERVER_CLIENT = 'run_server_client'


# 正文与展示意图分离。

_OPEN = {'{': '}', '[': ']'}


def strip_whole_fence(text: str) -> str:
    """仅剥离一次完整包裹正文的代码围栏；局部围栏或不合法的语言标签保持原样。"""
    t = text.strip()
    if not (t.startswith('```') and t.endswith('```')):
        return t
    body = t[len('```'):]
    nl = body.find('\n')
    if nl < 0:
        return t
    lang = body[:nl].strip()
    if len(lang) > 15 or (lang and not re.fullmatch(r'[A-Za-z0-9_+-]*', lang)):
        return t                      # 首行不是语言标签＝不是围栏，原样交回
    inner = body[nl + 1:]
    if inner.rstrip().endswith('```'):
        return inner.rstrip()[:-3].rstrip()
    return t


def _strip_marker_region(raw: str, start: int, end_rel: int | None) -> str:
    """按可确定的标记边界剥离展示意图，保留区外正文。

    双锚齐全时移除起始锚到最后结束锚的区间；缺结束锚时，从最后起始锚移除到文末。
    不删除区外内容，也不补全损坏 JSON。"""
    head = raw[:start].rstrip()
    if end_rel is None:
        return head
    tail = raw[start + len(MARKER_START) + end_rel + len(MARKER_END):].strip()
    if head and tail:
        return head + '\n\n' + tail
    return head or tail


def _balanced_object_end(seg: str) -> int | None:
    """从 seg[0]=='{' 起平衡扫描，返回完整顶层对象结束下标（含'}'）；失败=None。

    只跟踪 {}[] 嵌套与字符串/转义；深度归零即完整对象收口，出现提前闭合（深度负）=None。
    不补括号、不重排、不修复。
    """
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(seg):
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in _OPEN:
            depth += 1
        elif ch in _OPEN.values():
            depth -= 1
            if depth == 0:
                return i + 1
            if depth < 0:
                return None
    return None


def parse_display_intent(raw_final_answer: str) -> dict:
    """原始终答 → {status, intent, business_text, note, marker_zone_stripped}。

    - status ∈ exact / salvaged / failed（见模块常量）；
    - intent＝解析出的 JSON 对象（failed 时为 None）；结构合法性（顶层 dict、blocks 数组、
      每块 dict）在此统一校验，不合格＝failed（不进入送检，与字段核验分离）；
    - business_text＝**剔除标记区后**的业务正文；正式清洗链只应喂这份正文（接线前置 1）。
      **解析失败同样返回剥离后的正文**（损坏 JSON 与标记本身不得随降级件交回用户；
      剥离边界规则见 `_strip_marker_region`）；未命中起始锚时 business_text=原文
      （`marker_zone_stripped=False`）；
    - note＝失败/容错原因（审计用，原样保留，不美化）。
    """
    out = {'status': STATUS_FAILED, 'intent': None, 'business_text': raw_final_answer,
           'note': None, 'marker_zone_stripped': False}
    raw = str(raw_final_answer or '')
    start = raw.rfind(MARKER_START)
    if start < 0:
        out['note'] = 'marker_missing'
        return out
    after = raw[start + len(MARKER_START):]
    end = after.find(MARKER_END)
    # 按标记区边界剥离意图，保留结束标记之后的业务正文；解析失败也不泄漏损坏 JSON。
    out['business_text'] = _strip_marker_region(raw, start, end if end >= 0 else None)
    out['marker_zone_stripped'] = True
    if end >= 0:
        body = strip_whole_fence(after[:end])
        status = STATUS_EXACT
        rest_after = after[end + len(MARKER_END):]
    else:
        body = strip_whole_fence(after)
        status = STATUS_SALVAGED
        rest_after = ''
    if not body.startswith('{'):
        out['note'] = 'no_json_object_start'
        return out
    if status == STATUS_SALVAGED:
        end_idx = _balanced_object_end(body)
        if end_idx is None:
            out['note'] = 'unbalanced_json'
            return out
        if body[end_idx:].strip():
            out['note'] = 'trailing_content_after_json'
            return out
        body = body[:end_idx]
    try:
        obj = json.loads(body)
    except Exception as exc:                        # noqa: BLE001  解析失败如实登记
        out['note'] = 'invalid_json: %s' % exc
        return out
    if not _valid_intent(obj):
        out['note'] = 'intent_shape_invalid'
        return out
    if rest_after.strip():
        out['note'] = 'trailing_after_end_marker'
    out.update({'status': status, 'intent': obj})
    return out


def _valid_intent(obj) -> bool:
    """顶层结构校验（确定性、最小）：dict＋blocks 数组（可为空）＋每块 dict。"""
    if not isinstance(obj, dict):
        return False
    blocks = obj.get('blocks')
    if not isinstance(blocks, list):
        return False
    return all(isinstance(b, dict) for b in blocks)


def _list_or_empty(value) -> list:
    """模型声明面归一：list 才保留（标量笔误按字符迭代会把 'E1' 拆成 'E'/'1' 污染集合；
    非 list 视为缺省空列表，单块畸形不炸整份展示）。"""
    return value if isinstance(value, list) else []


def _block_calculation(block: dict) -> dict:
    """calculation 归一：非 dict（标量/数组笔误）按缺省 {}（`.get` 不再 AttributeError）。"""
    calc = block.get('calculation')
    return calc if isinstance(calc, dict) else {}


def intent_handles(intent: dict) -> list[str]:
    """展示意图引用集合（basis ∪ calculation.input_handles）按 run 去重。

    这就是「独立送检」的输入：与正文 `extract_refs`（只认 [E#] 括注）完全分离。
    已退出字段（checks 等）被忽略，其引用的 handle 不收集、不触发额外查询。
    """
    out: list[str] = []

    def _add(v) -> None:
        s = str(v or '').strip()
        if s and s not in out:
            out.append(s)

    for b in ((intent or {}).get('blocks') or []):
        if not isinstance(b, dict):
            continue
        for h in _list_or_empty(b.get('basis_handles')):
            _add(h)
        for h in _list_or_empty(_block_calculation(b).get('input_handles')):
            _add(h)
    return out


# ---------- 1b. 顶层 answer_line 读取（模型自报首屏短结论，纯字段读取） ----------

def extract_answer_line(intent: dict | None) -> str | None:
    """展示意图 JSON 顶层的可选 `answer_line` 原文（模型自报的首屏直接回答定位句）。

    只读字段并核对最小形状（非空字符串，去首尾空白）；**不做**任何出处核验——与
    `report.summary` 的逐字子串校验在 report 装配面做（`core.wire_contract.report_with_answer_line`，
    绑定清洗与守卫后的同一份可见正文）。意图缺失/解析失败/字段缺失或非串 = None
    （字段不交付；与 blocks 相互独立：blocks=[] 的意图同样可携带有效 answer_line）。
    """
    if not isinstance(intent, dict):
        return None
    v = intent.get('answer_line')
    if not isinstance(v, str):
        return None
    return v.strip() or None


def answer_line_from_final(final_text: str) -> str | None:
    """原始终答 → 顶层 answer_line 原文（parse_display_intent + extract 的便捷组合）。

    供 run 流收尾的 report 装配点调用；与 `build_display_blocks` 消费同一解析单点
    （`parse_display_intent`），无标记/解析失败/意图形状非法一律 None，不抛错、不降级正文。
    """
    return answer_line_signals(final_text)[0]


def answer_line_signals(final_text: str) -> tuple[str | None, bool]:
    """一次提取 answer_line 原文及是否存在展示意图区。

    存在起始标记即视为尝试提供意图，包含 JSON 残缺情形；仅有意图区时才允许报告提取缺失的短结论。"""
    parsed = parse_display_intent(str(final_text or ''))
    return extract_answer_line(parsed.get('intent')), bool(parsed.get('marker_zone_stripped'))


# 统一回查内容中的引用类型。

def _kind_from_ref(ref: str) -> str:
    parts = str(ref or '').split(':')
    if len(parts) >= 3 and parts[0] == 'pqev':
        return parts[2]
    if str(ref or '').startswith('pev:'):
        return 'evidence'
    return ''


def normalize_resolve_items(items):
    """按 canonical 引用类型补齐回查条目的 kind，不就地修改登记响应。

    集合的 median/rank 是比较模式，保留为 kind_name；不能将其误当作引用类型。"""
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            out.append(it)
            continue
        content = it.get('content')
        kind = _kind_from_ref(str(it.get('ref') or ''))
        kind_name = None
        # value 子集品类合计与完整品类合计在展示/事实面共用「品类汇总」形状；
        # 原引用类型留在 kind_name（键名不丢，页面/调试面仍可区分）。
        if kind == 'category_selected_total':
            kind_name = kind
            kind = 'category_total'
        if (isinstance(content, dict) and it.get('status') == 'resolved' and kind
                and str(content.get('kind') or '') != kind):
            patched = {'kind': kind}
            if kind_name:
                patched['kind_name'] = kind_name
            if content.get('kind') and kind in COMPARE_COLLECTION_KINDS:
                patched['kind_name'] = content.get('kind')   # median/rank 基准语义留档
            it = dict(it, content=dict(content, **patched))
        out.append(it)
    return out


class SourceUnavailable(Exception):
    """账本失效/传输失败：调用方必须降级（display_blocks=None）并如实登记，不得伪造核验。"""


class ClientRunSource:
    """正式回查通道：同一 run 的服务层 ToolServerClient（:8099 本 run 内存账本）。"""

    channel = CHANNEL_SERVER_CLIENT

    def __init__(self, client):
        self._client = client

    def guard(self, run_id: str, handles: list[str]) -> dict:
        try:
            g = self._client.qev_guard(run_id, handles)
        except Exception as exc:                     # noqa: BLE001  传输失败如实降级
            raise SourceUnavailable('intent 送检传输失败：%s' % type(exc).__name__) from exc
        if not isinstance(g, dict):
            raise SourceUnavailable('intent 送检响应形状不符（非 dict）')
        if g.get('guard_error') == 'root_context_not_found':
            raise SourceUnavailable('run %s 服务端注册账本已失效（root_context_not_found）' % run_id)
        return g

    def resolve(self, run_id: str, refs: list[str]) -> dict:
        try:
            r = self._client.evidence_resolve(run_id, refs)
        except Exception as exc:                     # noqa: BLE001
            raise SourceUnavailable('intent 回查传输失败：%s' % type(exc).__name__) from exc
        if not isinstance(r, dict) or not r.get('ok'):
            code = (r or {}).get('error_code') if isinstance(r, dict) else '非 dict'
            raise SourceUnavailable('run %s evidence_resolve 不可用：%s' % (run_id, code))
        return dict(r, items=normalize_resolve_items(r.get('items')))


# 独立核验引用、回查原值并计算封闭集合结果。

# cohort_list 块类型（cohort_filter 权威集合的展示块）。进入 `BLOCK_TYPES`——
# 该常量是「Prompt 展示意图段已登记的 type 封闭集合」（test_uix08 钉：每个成员必须出现在
# Prompt 文本里），已把 cohort_list 的生成入口写进 `core.prompts` 的展示意图段，两侧
# 同步。服务端核验面对该 type 的处理在本模块闭环（下方 `_verify_cohort_block`），消费面
# （Wire/UI）按 type 适配；它不进 `calculation.kind`/`checks` 封闭集合（集合块的复算与权威
# 对账由专用路径承载，不借通用复算面）。
COHORT_BLOCK_TYPE = 'cohort_list'

BLOCK_TYPES = ('metric_change', 'comparison', 'candidate_list', COHORT_BLOCK_TYPE)

# 权威集合绑定判据的词值（内部段 `binding`）：authority=直接采用权威集合；corrected=模型
# 声明的名单/计数与权威结果不一致、已由服务端权威集合填充纠正（结构化列表恒按权威呈现）。
COHORT_BINDING_AUTHORITY = 'authority'
COHORT_BINDING_CORRECTED = 'corrected'
# 集合等值对账状态：模型未声明名单/计数 / 声明与权威一致 / 声明不一致已纠正。
COHORT_RECON_NOT_DECLARED = 'not_declared'
COHORT_RECON_EQUAL = 'equal'
COHORT_RECON_CORRECTED = 'corrected'

HANDLE_VERIFIED = 'verified'
HANDLE_NOT_IN_RUN = 'not_in_run'            # 未在本 run 注册账本签发（伪造/跨 run）
HANDLE_GUARD_REJECTED = 'guard_rejected'    # 已登记但守卫拒绝
HANDLE_RESOLVE_MISSING = 'resolve_missing'  # 已签发但回查不到内容（不伪读数）

# ---------- 业务面局部说明 vs 内部技术诊断 ----------
# `data_issues` 是**业务面**（下发 Wire、就近展示）：只写对经营阅读有影响的结果/范围限制，
# 不出现 handle/字段名/合同措辞/计算实现细节。技术成因（未签发句柄、封闭集合外 kind、
# 绑定失败的具体引用）只进 `internal_issues`（内部核验面，不上 Wire），供排查与审计。
ISSUE_BASIS_UNVERIFIED = '部分依据未能核验，相关数据未纳入本次展示。'
ISSUE_CALC_UNAVAILABLE = '缺少可用数据，暂无法计算派生结果。'
ISSUE_CALC_INVALID = '该项派生结果暂无法计算。'
ISSUE_COHORT_UNBOUND = '本次筛选结果暂时无法对应到可核验的筛选数据。'
ISSUE_COHORT_MIXED = '本次筛选结果的名单依据不完整，暂不展示完整名单。'
ISSUE_COHORT_AMBIGUOUS = '本次筛选结果对应多组筛选数据，暂时无法确定名单。'
ISSUE_COHORT_INCOMPLETE = '筛选名单可能不完整，暂不作为完整名单展示。'


def _calc_unavailable_issue(kind) -> str:
    """派生结果无法产出时的业务提示（按封闭集合 kind 给业务名；不暴露实现细节）。"""
    label = {'rank': '排名', 'median': '中位数',
             'direction_counts': '涨跌数量'}.get(str(kind or ''))
    return '缺少可用数据，暂无法计算%s。' % label if label else ISSUE_CALC_UNAVAILABLE


def _add_issue(bucket: list, text: str) -> None:
    """业务说明去重追加（同一块不重复铺同一句话）。"""
    if text and text not in bucket:
        bucket.append(text)


def _handle_sort_key(x):
    """按 E 后数字排序短引用；其它引用稳定排尾，排序不代替签发核验。"""
    s = str(x)
    return (0, int(s[1:]), '') if s[:1] == 'E' and s[1:].isdigit() else (1, 0, s)


def _block_handles(block: dict) -> list[str]:
    out: list[str] = []

    def _add(v):
        s = str(v or '').strip()
        if s and s not in out:
            out.append(s)

    for h in _list_or_empty(block.get('basis_handles')):
        _add(h)
    for h in _list_or_empty(_block_calculation(block).get('input_handles')):
        _add(h)
    return out


def _compare_group_ids(group) -> list[str]:
    """读取比较组成员：高于、持平、低于组为 ID 数组，缺失组为含 product_id 的对象数组。"""

    out: list[str] = []
    for x in (group or []):
        pid = x.get('product_id') if isinstance(x, dict) else x
        if str(pid or ''):
            out.append(str(pid))
    return out


# 日期窗口引用使用真实起止日期作为身份，不要求目录 period_id。
DATE_WINDOW_KINDS = ('window_object', 'window_diff', 'window_compare', 'day_series')

# **整体集合引用**族——回查内容＝Data 的 set_compare / window_set_compare 响应整体
# （pctx_query._result_qev_keys 的 `compare:` / `window_compare:` 键，`oagent_ctx._locate_ref_content`
# 对待。工具权威 `ranking` 与成员范围只在这一族上取；逐行引用（compare_row / window_compare_row /
# *_missing）只承载单个成员，不承担整体榜单。
COMPARE_COLLECTION_KINDS = ('compare', 'window_compare')

# 单期集合合计的集合级引用类型；value `aggregate='sum'` 响应的 `single_period_totals`
# 以 `kind='set_sum'` + `metric` 区分 /；不按 category+metric 定位、不挂在商品行引用上。
SINGLE_PERIOD_SUM_KINDS = ('set_sum',)


def _tool_ranking_rows(ranking):
    """原样投影数据工具的名次、顺序和值，不重排、不补造成员；空榜单与无可用排名分别处理。"""
    if not isinstance(ranking, list):
        return None
    out = []
    for it in ranking:
        if not isinstance(it, dict):
            continue
        out.append({'rank': it.get('rank'),
                    'product_id': (str(it['product_id']) if it.get('product_id') else None),
                    'value': it.get('value')})
    return out


def _tool_missing_group(missing_group):
    """Data rank 集合的原缺失成员；非数组/旧 payload 不宣称缺失集合为空。"""
    if not isinstance(missing_group, list):
        return None
    keys = ('product_id', 'value', 'state', 'reason')
    return [{key: item.get(key) for key in keys}
            for item in missing_group if isinstance(item, dict)]


def _window_identity(block) -> dict | None:
    """日期窗口块 → 展示用窗口身份 {start_date,end_date,length_days}；非日期块=None（不编造）。

    三类真实形状都要吃下（同一函数覆盖，不建第二套时间解析）：
    - 日级聚合块本体（`window_values`/`window_diff` 的 results 元素、`base`/`target` 侧）：
      直接带 `period_start_date`/`period_end_date`/`period_length_days`；
    - `window:` / `window_compare:` 引用：窗口在 `window` 键（`_window_identity` 输出形状）；
    - `day_series:` 引用：窗口在 `range` 键（该窗口的汇总块）。
    """
    if not isinstance(block, dict):
        return None
    # 三类持有者按序尝试：块本体（window_values/window_diff 的 results 元素、base/target 侧）、
    # `window` 键（window:/window_compare: 引用）、`range` 键（day_series: 引用的窗口汇总块）。
    # 每类都可能是「日级聚合块」（period_start_date/period_end_date）或「窗口身份块」
    # （start_date/end_date）两种写法，故两种键名都看。
    for cand in (block, block.get('window'), block.get('range')):
        if not isinstance(cand, dict):
            continue
        start, end = cand.get('period_start_date'), cand.get('period_end_date')
        if not start and not end:
            start, end = cand.get('start_date'), cand.get('end_date')
        if not start and not end:
            continue
        length = cand.get('period_length_days', cand.get('length_days'))
        if length is None:
            length = cand.get('n_days')
        return {'start_date': start, 'end_date': end, 'length_days': length}
    return None


def _window_period_id(block) -> str | None:
    """日期窗口块 → 窗口身份串 `start~end`（Data 原样起止，非编造）；无日期=None。

    把日期窗口读数接入既有按 `period_id` 渲染/复算的展示面（`demo_ui/lib/trace.ts::periodLabel`
    目录查不到时原样显示该串＝真实日期区间；`_recompute_median_groups` 按窗口身份分组），
    不新造第二套时间标识、不伪造目录周期。
    """
    w = _window_identity(block)
    if not w or not (w.get('start_date') or w.get('end_date')):
        return None
    return '%s~%s' % (w.get('start_date'), w.get('end_date'))


def _fact_from_content(ref: str, content: dict) -> dict:
    """将回查内容归一为核验事实，缺字段保持 None。

    日期窗口使用真实起止日期作为身份；差值与百分比使用工具原值。
    日序列的汇总值、单位和状态取自 range，逐日缺值不补零。"""
    kind = str(content.get('kind') or '')
    f = {'ref': str(ref), 'kind': kind, 'metric': content.get('metric'),
         'product_id': (str(content['product_id']) if content.get('product_id') else None),
         'period_id': (str(content['period_id']) if content.get('period_id') else None),
         'value': content.get('value'), 'state': content.get('state'),
         'base': None, 'target': None, 'absolute': content.get('absolute_diff'),
         'percent': content.get('percent_diff'), 'direction': content.get('direction'),
         'tool_median': None, 'unit': content.get('unit'),
         'window': None, 'day_series': None,
         # 整体集合只有在原比较模式为 rank 时读取工具排名；该排名不能与局部复算名次混用。
         'tool_ranking': None,
         'tool_missing_group': None,
         # 集合成员范围与覆盖（selected/usable/missing 计数，Data 已核实口径）；
         # 与 ranking 同源、只在整体集合引用上取，用于数据区如实标注「本次选中集合」。
         'compare_scope': None,
         # 单期集合合计引用（`kind='set_sum'`）：回查内容＝Data 的 single_period_totals
         # 条目（请求集合、指标、合计值、覆盖计数、状态分类、周期/窗口时间身份）。
         'single_period_totals': None}
    if kind in DATE_WINDOW_KINDS:
        f['window'] = _window_identity(content)
        if not f['period_id']:
            f['period_id'] = _window_period_id(content)
    if kind == 'window_diff':
        base = content.get('base') or {}
        target = content.get('target') or {}
        f['window'] = None          # 两段窗口各自归属，不在事实级给单一窗口身份
        f['base'] = {'period_id': _window_period_id(base), 'value': base.get('value')}
        f['target'] = {'period_id': _window_period_id(target), 'value': target.get('value')}
    if kind == 'diff':
        base = content.get('base') or {}
        target = content.get('target') or {}
        f['base'] = {'period_id': base.get('period_id'), 'value': base.get('value')}
        f['target'] = {'period_id': target.get('period_id'), 'value': target.get('value')}
    if kind == 'day_series':
        # 连续日读数的**窗口汇总**在真实响应里位于 `range`（Data 的日级聚合块），行顶层没有
        # `value`/`unit`/`state`（恒为 None）。不从 `range` 取会让主数据块把汇总值显示成「—」
        # 并丢掉指标单位——而证据详情已按 `range` 正确投影，两处会不一致。
        rng = content.get('range') if isinstance(content.get('range'), dict) else {}
        f['value'] = rng.get('value')
        f['state'] = rng.get('state')
        f['unit'] = rng.get('unit') or f['unit']
        days = []
        for d in (content.get('days') or []):
            if not isinstance(d, dict):
                continue
            days.append({'date': d.get('date'), 'value': d.get('value'),
                         'state': d.get('state')})
        f['day_series'] = days or None
    if kind in COMPARE_COLLECTION_KINDS and content.get('median') is not None:
        f['tool_median'] = {'median': content.get('median'),
                            'label': '工具返回（非本模块复算）'}
    if kind == 'compare':
        # compare 元素的 **确定性分组关系**（Data 已算好的 high/equal/low/missing 成员，
        # 见 qctx_period set_compare 响应）＋其嵌套周期（顶层无 period_id，周期在 content['period']）
        # ＋成员集合规模（`objects`，基准身份的一部分）。只用于本模块的实质性比较核验
        # （对齐/位置/多基准区分），不进 `_DISPLAY_FACT_KEYS` 下发面。
        period = content.get('period') if isinstance(content.get('period'), dict) else {}
        f['compare_period_id'] = str(period.get('period_id') or '') or None
        f['compare_groups'] = {k: _compare_group_ids(content.get('%s_group' % k))
                               for k in ('high', 'equal', 'low', 'missing')}
        objs = content.get('objects')
        f['compare_members_n'] = len(objs) if isinstance(objs, list) else None
    if kind in COMPARE_COLLECTION_KINDS:
        # 整体集合引用的权威排名与成员范围。只在原比较模式为 rank 时取工具 `ranking`
        # （median 内容不含 ranking，取到 None＝如实无榜单，不伪造）；族内一律记录
        # Data 的成员范围（`objects` 全集、可用/缺失计数），供数据区如实标注「本次选中集合」。
        # `kind_name` 缺省（旧 SSE/未归一）时保守不取排名——「无排名引用不伪造榜单」。
        if str(content.get('kind_name') or '') == 'rank':
            f['tool_ranking'] = _tool_ranking_rows(content.get('ranking'))
            f['tool_missing_group'] = _tool_missing_group(content.get('missing_group'))
        objects = content.get('objects')
        f['compare_scope'] = {
            'selected_n': (len(content['objects']) if isinstance(content.get('objects'), list)
                           else None),
            'product_ids': list(objects) if isinstance(objects, list) else None,
            'usable_n': content.get('usable_count'),
            'missing_n': content.get('missing_count'),
            'kind_name': str(content.get('kind_name') or '') or None}
    if kind in SINGLE_PERIOD_SUM_KINDS:
        # value aggregate='sum' 的单期集合合计引用；只搬运 Data 的整体 sum、实际成员、
        # 覆盖/状态与周期或真实日期窗口，不按目录品类拆组，也不做完整品类推断。
        f['single_period_totals'] = {
            'metric': content.get('metric'),
            'metric_name': content.get('metric_name'),
            'unit': content.get('unit'),
            'aggregate': content.get('aggregate'),
            'sum': content.get('sum'),
            'complete': content.get('complete'),
            'sum_scope': content.get('sum_scope'),
            'selected_n': content.get('selected_n'),
            'comparable_n': content.get('comparable_n'),
            'non_comparable_n': content.get('non_comparable_n'),
            'non_comparable_by_state': content.get('non_comparable_by_state'),
            'product_ids': content.get('product_ids'),
            'period_id': content.get('period_id'),
            'start_date': content.get('start_date'),
            'end_date': content.get('end_date'),
            'window_identity': content.get('window_identity'),
            'note': content.get('summary_note'),
        }
    if kind == 'category_total' and str(content.get('shape') or '') == 'value':
        # 单期品类合计引用（完整品类或本次子集）——只补真实日期窗口身份；
        # 合计值本身走通用 fact `value`（Data 原值），不在此另造键面。
        f['window'] = _window_identity(content)
    return f


def _tool_rankings(facts: dict) -> list[dict]:
    """已核验事实里的**整体集合排名引用** → 数据区工具排名（零复算、纯搬运）。

    输入只认 `tool_ranking` 非 None 的 fact（＝`kind_name='rank'` 且 Data 明确返回排名数组的
    compare / window_compare 整体引用，见 `_fact_from_content`）；空数组仍是有效的全缺失 rank
    结果。输出每条聚合该引用的成员范围、缺失成员、时间身份与引用关联：
    `kind`＝引用类型（compare / window_compare）、`metric`、`period_id`（目录周期 ID 或真实
    日期窗口身份串）、`unit`、完整 `product_ids`、`selected_n`/`usable_n`/`missing_n`、
    Data 原 `missing_group`、`handle`/`ref` 与 `rows`（Data 原名次/商品/值，顺序不变）。
    中位数引用不产出条目——无排名引用不伪造榜单。
    """
    out = []
    for h in sorted(facts, key=_handle_sort_key):
        f = facts[h]
        rows = f.get('tool_ranking')
        if rows is None:
            continue
        scope = f.get('compare_scope') if isinstance(f.get('compare_scope'), dict) else {}
        out.append({
            'kind': str(f.get('kind') or ''),
            'kind_name': scope.get('kind_name'),
            'metric': f.get('metric'),
            'unit': f.get('unit'),
            'period_id': (f.get('period_id') or f.get('compare_period_id')),
            'window': f.get('window'),
            'product_ids': scope.get('product_ids'),
            'selected_n': scope.get('selected_n'),
            'usable_n': scope.get('usable_n'),
            'missing_n': scope.get('missing_n'),
            'missing_group': f.get('tool_missing_group'),
            'handle': f.get('handle'),
            'ref': f.get('ref'),
            'rows': [dict(r) for r in rows],
        })
    return out


def _classify_diff(f: dict) -> str:
    """使用 Data 基于有效 absolute 给出的方向；没有方向的读数归入 other。"""
    direction = f.get('direction')
    return direction if direction in ('up', 'down', 'flat') else 'other'


def _recompute_median_groups(facts: dict, input_handles: list[str] | None,
                             declared_periods: list[str] | None = None) -> list[dict]:
    """仅按 calculation.input_handles 声明的输入集合计算中位数。

    basis 只是支撑引用，不能据此扩充统计范围；非 ok 或无值成员披露但不参与。
    声明周期时只使用范围内的单期读数，错配事实仍保留在核验记录中。"""
    if not input_handles:
        return []
    allowed = {str(h) for h in input_handles}
    declared = {str(p) for p in (declared_periods or [])}
    groups: dict[tuple, dict] = {}
    for h in sorted(facts, key=_handle_sort_key):
        if str(h) not in allowed:
            continue
        f = facts[h]
        if f.get('kind') != 'object' or not f.get('period_id'):
            continue
        if declared and str(f['period_id']) not in declared:
            continue
        key = (str(f.get('metric')), str(f['period_id']))
        g = groups.setdefault(key, {'metric': f.get('metric'), 'period_id': f['period_id'],
                                    'members': [], 'excluded': []})
        v = f.get('value')
        if f.get('state') == 'ok' and isinstance(v, (int, float)):
            g['members'].append({'handle': h, 'product_id': f.get('product_id'), 'value': v})
        else:
            g['excluded'].append({'handle': h, 'product_id': f.get('product_id'),
                                  'state': f.get('state')})
    out = []
    for g in groups.values():
        vals = sorted(m['value'] for m in g['members'])
        g['n'] = len(vals)
        g['median'] = median(vals) if vals else None
        g['basis'] = 'server_recomputed'
        g['members'] = sorted(g['members'], key=lambda m: m['value'])
        out.append(g)
    return out


def _recompute_directions(facts: dict) -> dict:
    """方向计数＝**块级读数**：块内全部已核验 diff 事实（含 basis 携带的跨引用 diff——
    verify_block 对 basis 跨指标引用显式 warning 不降级，方向读数同理消费）。
    与 `_recompute_median_groups` 只按 calculation.input_handles 统计的**声明集合口径
    有意不同**：中位数的统计范围必须由声明集收口，方向计数描述的是整块分析对象集
    （10-23 审查 P2-20 复核结论＝设计意图，test_uix06_phase2 C 类真实形状钉住）。"""
    counts = {'up': 0, 'down': 0, 'flat': 0}
    other = []
    n = 0
    for h in sorted(facts, key=_handle_sort_key):
        f = facts[h]
        if f.get('kind') != 'diff':
            continue
        n += 1
        cls = _classify_diff(f)
        if cls == 'other':
            other.append({'handle': h, 'state': f.get('state'),
                          'note': 'Data 未提供有效方向：不计涨跌'})
        else:
            counts[cls] += 1
    if n == 0:
        return {}
    return dict(counts, other=other, n=n, basis='server_recomputed')


# 块内可比读数的服务端复算合计（仅 comparison 块；只处理可加总的 /——
# 比率的合计无业务含义，卡面明令不加总；模型/前端不得自行求和）。
AGGREGATE_METRICS = ('M1', 'M2')
# 品类合计接受目录周期 diff 和真实日期窗口 diff。
AGGREGATE_DIFF_KINDS = ('diff', 'window_diff')


def _is_fact_num(v) -> bool:
    """有限数值（bool 是 int 子类，排除；None/字符串/unknown 串一律不算）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _recompute_group_totals(block: dict, facts: dict, subjects: list[str],
                            metrics_declared: list[str] | None = None,
                            periods_declared: list[str] | None = None) -> list[dict]:
    """按目录品类、指标与两期身份，对已回查的 M1/M2 比较读数分别求和。

    声明对象必须完整且不重复，每个对象须能定位目录品类；不跨品类混合汇总。
    每组仅接受唯一的比较窗口，且必须符合声明指标与周期范围；真实日期窗口不与目录周期 ID 求交。
    只有两期值和绝对差均为数值的成员参与合计，其余按原因披露；各指标独立统计覆盖。
    子集合计不代表完整目录品类，复算不为模型的自然语言合计提供句级核验。"""
    if str(block.get('type') or '') != 'comparison':
        return []
    wanted = [str(s) for s in (subjects or [])]
    if not wanted or len(set(wanted)) != len(wanted):
        return []                      # 声明对象缺失/重复：聚合口径不自洽，不产出
    wanted_set = set(wanted)
    category_of = {str(p['product_id']): p.get('category') for p in PRODUCTS}
    if any(not category_of.get(s) for s in wanted):
        return []                      # 任一声明对象无目录品类：无法可信归属，整块不产出
    declared_metrics = {str(m) for m in (metrics_declared or [])}
    declared_periods = {str(p) for p in (periods_declared or [])}
    selected = [facts[h] for h in sorted(facts, key=_handle_sort_key)
                if facts[h].get('kind') in AGGREGATE_DIFF_KINDS
                and str(facts[h].get('metric') or '') in AGGREGATE_METRICS
                and str(facts[h].get('product_id') or '') in wanted_set]
    # 品类拆分：一个品类一组，各自独立走下面的完整性/窗口/口径判据（不跨品类合计）。
    by_category: dict[str, set[str]] = {}
    for s in wanted:
        by_category.setdefault(category_of[s], set()).add(s)
    out: list[dict] = []
    for category, cat_subjects in by_category.items():
        for metric in AGGREGATE_METRICS:
            if declared_metrics and metric not in declared_metrics:
                continue               # 指标不在块声明口径内：不产该指标合计
            members = [f for f in selected
                       if str(f.get('metric') or '') == metric
                       and str(f.get('product_id') or '') in cat_subjects]
            if len(members) < 2:
                continue               # 单对象品类块的"合计"=该对象自身读数，非聚合，不发冗余字段
            windows = {(str((f.get('base') or {}).get('period_id') or ''),
                        str((f.get('target') or {}).get('period_id') or '')) for f in members}
            windows = {w for w in windows if w[0] and w[1]}
            if len(windows) != 1:
                continue               # 期间身份缺失或同一指标两套窗口：不产该条
            base_period_id, target_period_id = windows.pop()
            # 日期窗口身份（`start~end`）与目录周期 id 不同域——两侧都是日期窗口时不与
            # 周期声明集求交（否则「4 月比 3 月」这类窗口合计会因块声明了目录周期而被静默丢掉）。
            window_identity = ('~' in base_period_id) or ('~' in target_period_id)
            if declared_periods and not window_identity \
                    and not ({base_period_id, target_period_id} & declared_periods):
                continue               # 两侧都不在声明周期口径内：不产出
            pids = [str(f.get('product_id') or '') for f in members]
            if len(set(pids)) != len(pids) or set(pids) != cat_subjects:
                continue               # 同一商品重复计入 / 该品类声明对象无对应已核验读数：受限
            base_sum = target_sum = 0.0
            comparable = 0
            by_state: dict[str, int] = {}
            units = set()
            for f in members:
                if f.get('unit'):
                    units.add(str(f['unit']))
                if (_is_fact_num(f.get('absolute'))
                        and _is_fact_num((f.get('base') or {}).get('value'))
                        and _is_fact_num((f.get('target') or {}).get('value'))):
                    comparable += 1
                    base_sum += f['base']['value']
                    target_sum += f['target']['value']
                else:
                    st = str(f.get('state') or 'unknown')
                    by_state[st] = by_state.get(st, 0) + 1
            entry = {'category': category, 'metric': metric,
                     'unit': (units.pop() if len(units) == 1 else None),
                     'base_period_id': base_period_id, 'target_period_id': target_period_id,
                     'subjects_n': len(cat_subjects), 'comparable_n': comparable,
                     'non_comparable_n': len(members) - comparable,
                     'non_comparable_by_state': dict(sorted(by_state.items())),
                     'base_sum': None, 'target_sum': None, 'absolute_change': None,
                     'percent_change': None, 'percent_change_state': 'no_comparable_products',
                     'sum_scope': 'comparable_products_only', 'basis': 'server_recomputed'}
            if comparable:
                entry['base_sum'] = round(base_sum, 4)
                entry['target_sum'] = round(target_sum, 4)
                entry['absolute_change'] = round(target_sum - base_sum, 4)
                if base_sum:
                    entry['percent_change'] = round((target_sum - base_sum) / base_sum * 100.0, 4)
                    entry['percent_change_state'] = 'ok'
                else:
                    entry['percent_change_state'] = 'zero_denominator'
            out.append(entry)
    return out


def _recompute_ranks(facts: dict, median_groups: list[dict], subjects: list[str]) -> list[dict]:
    """subjects 在（指标,周期）分组内的名次：rank_desc=自高第几、rank_asc=自低第几。
    名次只来自已回查原始值与成员集合；模型文字名次一律不采信。范围口径＝**块级**：
    名次分母（成员集合）来自 median 的声明集统计，被排名对象由 `subjects` 声明过滤——
    与 median 的声明集合口径分工有意不同（10-23 审查 P2-20 复核结论＝设计意图）。"""
    out = []
    for g in median_groups:
        vals = [m['value'] for m in g['members']]
        for h in sorted(facts, key=_handle_sort_key):
            f = facts[h]
            if (f.get('kind') != 'object' or str(f.get('metric')) != str(g['metric'])
                    or str(f.get('period_id')) != str(g['period_id'])):
                continue
            pid = f.get('product_id')
            if subjects and str(pid or '') not in [str(s) for s in subjects]:
                continue
            v = f.get('value')
            if v is None or not isinstance(v, (int, float)):
                continue
            rank_desc = 1 + sum(1 for x in vals if x > v)
            rank_asc = 1 + sum(1 for x in vals if x < v)
            out.append({'product_id': pid, 'handle': h, 'metric': g['metric'],
                        'period_id': g['period_id'], 'value': v, 'set_n': g['n'],
                        'rank_desc': rank_desc, 'rank_asc': rank_asc,
                        'basis': 'server_recomputed'})
    return out


def _cohort_unknown_reasons(row: dict) -> list:
    """未知成员行各条件值里的 'unknown(原因)' 去重排序（数值/普通串不是原因）。"""
    out: list[str] = []
    for v in row.values():
        for t in unknown_reason_tokens(v):
            if t not in out:
                out.append(t)
    return sorted(out)


def _cohort_readings(row: dict, conditions: list) -> list:
    """命中成员行 → 与 conditions 对齐的读数组（键形 `{metric}_{field}`，缺失=None 不代 0）。"""
    out = []
    for c in conditions:
        metric, field = str(c.get('metric') or ''), str(c.get('field') or '')
        out.append({'metric': metric, 'field': field,
                    'value': row.get('%s_%s' % (metric, field))})
    return out


def _cohort_scope_signature(scope: dict | None) -> str | None:
    """范围身份指纹：Data 响应里**已核实的完整 scope 成员集** → 顺序无关的稳定标识。

    为什么不能从命中清单推：完整范围＝命中 ∪ 未命中 ∪ 无法判定，而未命中成员不在响应名单里
    （只有非命中计数）。仅凭 `scope_count` + 命中/未知名单无法区分「同一批命中、未命中成员不同」
    的两个查询——展示层会把它俩当同一权威集合去重（真相被隐藏）。故范围身份必须由成员集本身
    给出，且只取 Data 已核实字段（模型自由声明的 scope/matches 不参与）：

    - `benchmark_all`：成员＝当前视图目录全池，身份由 kind 唯一确定（同一 run 视图内全池一致）
      → `pool:benchmark_all`；
    - `product_ids`：成员＝Data 规范化后的完整 ID 列表（含未命中成员），取**排序后**成员集的
      sha1 指纹——顺序不同但成员集相同＝同指纹，成员集不同＝必不同；
    - 成员集不可得（旧 Data 形态/畸形 scope）＝ None，消费侧按「信息不足」保守处理，不推定等价。
    """
    if not isinstance(scope, dict):
        return None
    kind = scope.get('kind')
    if kind == 'benchmark_all':
        return 'pool:benchmark_all'
    ids = scope.get('product_ids')
    if kind == 'product_ids' and isinstance(ids, list):
        norm = '\n'.join(sorted(str(p) for p in ids))
        return 'ids:' + hashlib.sha1(norm.encode('utf-8')).hexdigest()
    return None


def _verify_cohort_block(*, run_id, index, block, source, handles, handle_status,
                         items, resolutions, unresolved,
                         basis_data_issues, basis_internal_issues) -> dict:
    """用单个权威集合引用核验并填充筛选列表。

    不得混入其它查询或对象引用；绑定不独立时不交付集合段。
    模型声明的成员与计数用于对账，发现不一致时记录问题，列表仍以权威结果填充。
    集合事实通过 cohort 下发，不伪装为逐对象 verified_facts。"""
    facts_cohort = []
    other_refs = []
    for h in handles:
        if handle_status.get(h) != HANDLE_VERIFIED:
            continue
        canon = str(resolutions.get(h) or '')
        it = items.get(canon)
        if it is not None and isinstance(it.get('content'), dict) \
                and str(it['content'].get('kind') or '') == 'cohort':
            facts_cohort.append((h, canon, it['content']))
        else:
            other_refs.append(h)

    data_issues = list(basis_data_issues)
    internal_issues = list(basis_internal_issues)
    if not facts_cohort:
        internal_issues.append(
            'cohort_list 未绑定到经核验的集合级引用（basis 需恰一条 cohort 查询引用；'
            '当前声明的 handle 均未核验或不指向集合级结果）')
        _add_issue(data_issues, ISSUE_COHORT_UNBOUND)
    elif other_refs:
        # 集合块只允许独立绑定本次筛选集合引用，混入其它引用时不交付集合段。
        internal_issues.append(
            'cohort_list basis 未独立绑定：除该次集合查询引用外还混入了 %d 条其他引用，'
            '不作为独立权威集合交付'
            % len(other_refs))
        _add_issue(data_issues, ISSUE_COHORT_MIXED)
    elif len(facts_cohort) > 1:
        internal_issues.append(
            'cohort_list 绑定歧义：块声明了 %d 条经核验集合引用，无法确定名单归属'
            '（不拿 A 查询的名单证明 B 查询）' % len(facts_cohort))
        _add_issue(data_issues, ISSUE_COHORT_AMBIGUOUS)

    section = None
    recon_status = COHORT_RECON_NOT_DECLARED
    if len(facts_cohort) == 1 and not basis_data_issues and not other_refs:
        h, canon, content = facts_cohort[0]
        matches = content.get('matches')
        unknown = content.get('unknown')
        match_count = content.get('match_count')
        scope_count = content.get('scope_count')
        unknown_count = content.get('predicate_unknown_count')
        counts_int = all(isinstance(x, int) and not isinstance(x, bool)
                         for x in (match_count, scope_count, unknown_count,
                                   content.get('nonmatch_count')))
        rows_ok = (isinstance(matches, list) and isinstance(unknown, list)
                   and all(isinstance(m, dict) and m.get('product_id') for m in matches)
                   and all(isinstance(u, dict) and u.get('product_id') for u in unknown))
        if content.get('members_complete') is not True or not counts_int or not rows_ok \
                or len(matches) != match_count or len(unknown) != unknown_count:
            internal_issues.append(
                '权威集合完整性对账失败（members_complete/成员数与计数不一致或成员行形状不符）：'
                '名单可能截断，不作为完整名单交付')
            _add_issue(data_issues, ISSUE_COHORT_INCOMPLETE)
        else:
            conditions = [c for c in (content.get('conditions') or []) if isinstance(c, dict)]
            match_rows = [{'product_id': str(m['product_id']),
                           'readings': _cohort_readings(m, conditions)}
                          for m in matches]
            unknown_members = [{'product_id': str(u['product_id']),
                                'reasons': _cohort_unknown_reasons(u)}
                               for u in unknown]
            # —— 模型声明 vs 权威集合：集合等值对账（缺失/额外/重复 + 计数核对）——
            # 非 ID 标量（dict 等）不构成商品 ID 声明，清洗时不进对账集合（权威集合照常填充）。
            declared_ids = None
            if isinstance(block.get('matches'), list):
                declared_ids = [str(x) for x in block['matches']
                                if isinstance(x, (str, int))]
            declared_match_count = (block.get('match_count')
                                    if isinstance(block.get('match_count'), int)
                                    and not isinstance(block.get('match_count'), bool)
                                    else None)
            declared_scope_count = (block.get('scope_count')
                                    if isinstance(block.get('scope_count'), int)
                                    and not isinstance(block.get('scope_count'), bool)
                                    else None)
            authority_set = {r['product_id'] for r in match_rows}
            missing_ids: list[str] = []
            extra_ids: list[str] = []
            duplicate_ids: list[str] = []
            if declared_ids is not None:
                seen: set[str] = set()
                for pid in declared_ids:
                    if pid in seen and pid not in duplicate_ids:
                        duplicate_ids.append(pid)
                    seen.add(pid)
                declared_set = set(declared_ids)
                missing_ids = [r['product_id'] for r in match_rows
                               if r['product_id'] not in declared_set]
                for pid in declared_ids:
                    if pid not in authority_set and pid not in extra_ids:
                        extra_ids.append(pid)
            if declared_ids is None and declared_match_count is None \
                    and declared_scope_count is None:
                recon_status = COHORT_RECON_NOT_DECLARED
            else:
                mismatch = bool(missing_ids or extra_ids or duplicate_ids)
                if declared_match_count is not None and declared_match_count != match_count:
                    mismatch = True
                if declared_scope_count is not None and declared_scope_count != scope_count:
                    mismatch = True
                recon_status = (COHORT_RECON_CORRECTED if mismatch
                                else COHORT_RECON_EQUAL)
            section = {
                'ref': canon, 'handle': h,
                'query_id': str(canon.split(':')[1]) if canon.count(':') >= 2 else '',
                'binding': (COHORT_BINDING_CORRECTED
                            if recon_status == COHORT_RECON_CORRECTED
                            else COHORT_BINDING_AUTHORITY),
                'time_mode': content.get('time_mode'),
                'period_id': content.get('period_id'),
                'base_period_id': content.get('base_period_id'),
                'target_period_id': content.get('target_period_id'),
                'scope_kind': (content.get('scope') or {}).get('kind'),
                'scope_count': scope_count,
                # 范围身份指纹：完整 scope 成员集（含未命中成员）的稳定标识，供展示层判「同一
                # 权威集合」。只取 Data 已核实字段；成员集不可得＝None（消费侧不推定等价）。
                'scope_signature': _cohort_scope_signature(content.get('scope')),
                'conditions': conditions,
                'ordered_by': content.get('ordered_by'),
                'match_count': match_count,
                'nonmatch_count': content.get('nonmatch_count'),
                'predicate_unknown_count': unknown_count,
                'confirmed_share_percent': content.get('confirmed_share_percent'),
                'share_denominator': content.get('share_denominator'),
                'members_complete': True,
                'match_rows': match_rows,
                'unknown_members': unknown_members,
                'declared': {'matches': declared_ids,
                             'match_count': declared_match_count,
                             'scope_count': declared_scope_count},
                'reconciliation': {
                    'status': recon_status,
                    'missing_ids': missing_ids, 'extra_ids': extra_ids,
                    'duplicate_ids': duplicate_ids,
                    'match_count_declared': declared_match_count,
                    'match_count_actual': match_count,
                    'scope_count_declared': declared_scope_count,
                    'scope_count_actual': scope_count},
            }

    return {
        'index': index,
        'type': COHORT_BLOCK_TYPE,
        'display': {'subjects': _list_or_empty(block.get('subjects')),
                    'metrics': _list_or_empty(block.get('metrics')),
                    'periods': _list_or_empty(block.get('periods'))},
        'handle_status': handle_status,
        'verification': {
            'refs': {'total': len(handles), 'verified_n': len(facts_cohort),
                     'unresolved': sorted(unresolved, key=_handle_sort_key)},
        },
        'verified_facts': [],
        'recomputed': {'median_by_group': [], 'direction_counts': {},
                       'ranks': [], 'tool_returned': [], 'group_totals': []},
        'cohort': section,
        'identity_warnings': [],
        'internal_issues': internal_issues,
        'data_issues': data_issues,
        'provenance': {'run_id': str(run_id), 'evidence_channel': source.channel,
                       'refs': {h: str(resolutions.get(h) or '') for h in handles}},
    }


def verify_block(run_id: str, index: int, block: dict, source,
                 guard: dict, resolve_resp: dict) -> dict:
    """单块核验＋复算（内部面读数；Wire 投影见 `core.wire_contract.display_blocks_wire`）。

    只做确定性事实对账与封闭集合复算；真实引用/口径/计算问题进块级 `data_issues`
    （减法 2026-10-15：无块级/整页可信评级，无模型 checks，无自由文本数字扫描）。
    """
    handles = _block_handles(block)
    accepted = {str(x) for x in (guard.get('accepted') or [])}
    resolutions = guard.get('handle_resolutions') or {}
    items = {str(it.get('ref')): it for it in (resolve_resp.get('items') or [])}

    facts: dict[str, dict] = {}
    handle_status: dict[str, str] = {}
    for h in handles:
        canon = str(resolutions.get(h) or '')
        if not canon:
            handle_status[h] = HANDLE_NOT_IN_RUN
            continue
        if canon not in accepted:
            handle_status[h] = HANDLE_GUARD_REJECTED
            continue
        it = items.get(canon)
        if it is None or it.get('status') != 'resolved' \
                or not isinstance(it.get('content'), dict):
            handle_status[h] = HANDLE_RESOLVE_MISSING
            continue
        f = _fact_from_content(canon, it['content'])
        f['handle'] = h
        facts[h] = f
        handle_status[h] = HANDLE_VERIFIED

    unresolved = [h for h, s in handle_status.items() if s != HANDLE_VERIFIED]
    data_issues = []
    internal_issues = []
    identity_warnings = []
    for h in _list_or_empty(block.get('basis_handles')):
        hs = handle_status.get(str(h))
        if hs and hs != HANDLE_VERIFIED:
            internal_issues.append('basis 引用未核验/缺原始值：%s（%s）' % (h, hs))
            _add_issue(data_issues, ISSUE_BASIS_UNVERIFIED)
    # cohort_list 走集合级专用核验（权威绑定/完整性门/集合等值对账），不进行级
    # fact 复算——在通用复算面之前分流，其正向下限由绑定判据承载。
    if str(block.get('type') or '') == COHORT_BLOCK_TYPE:
        return _verify_cohort_block(
            run_id=run_id, index=index, block=block, source=source,
            handles=handles, handle_status=handle_status, items=items,
            resolutions=resolutions, unresolved=unresolved,
            basis_data_issues=data_issues,
            basis_internal_issues=internal_issues)

    # 核对展示声明与证据中的对象、指标和期间（声明面归一：标量笔误按缺省空集，
    # 不按字符迭代污染声明集——单块畸形不炸整份展示）。
    metrics_declared = [str(x) for x in _list_or_empty(block.get('metrics'))]
    periods_declared = [str(x) for x in _list_or_empty(block.get('periods'))]
    subjects_declared = [str(x) for x in _list_or_empty(block.get('subjects'))]
    for h, f in facts.items():
        fm = str(f.get('metric') or '')
        if metrics_declared and fm and fm not in metrics_declared:
            # basis 允许带跨指标上下文引用（B 题用 /收缩解释 ）：登记 warning，
            # 不整块降级；已核验数据面每条 fact 自带 metric，前端按指标分行渲染即可。
            identity_warnings.append('引用读数指标不在块声明内：%s metric=%s ∉ metrics=%s'
                                     % (h, fm, metrics_declared))
        fps = set()
        if f.get('period_id'):
            fps.add(str(f['period_id']))
        for side in ('base', 'target'):
            if (f.get(side) or {}).get('period_id'):
                fps.add(str(f[side]['period_id']))
        if periods_declared and fps:
            declared = set(periods_declared)
            if f.get('kind') in AGGREGATE_DIFF_KINDS:
                # 两期比较只要一侧属于声明周期即可支持该期间判断；真实日期窗口不与目录周期 ID 求交。
                if not any('~' in p for p in fps) and not (fps & declared):
                    identity_warnings.append(
                        '周期口径错配（技术读数）：%s diff 两侧周期 %s 与 block.periods=%s 无交集'
                        % (h, sorted(fps), periods_declared))
            else:
                extra = fps - declared
                if extra:
                    identity_warnings.append(
                        '周期口径错配（技术读数）：%s 读数周期 %s ∉ block.periods=%s'
                        % (h, sorted(extra), periods_declared))
    calc = _block_calculation(block)
    # 复算的范围口径（10-23 审查 P2-20 复核结论＝设计意图，注释钉住差异）：
    # - median：只按 calculation.input_handles 声明集统计（basis 不能扩充中位数统计范围）；
    # - direction_counts / ranks：**块级读数**，消费块内全部已核验事实（含 basis 携带的
    #   跨引用——与「basis 跨指标引用 warning 不降级」同一哲学；UIX06 C 类真实形状钉住），
    #   名次分母仍来自 median 的声明集、被排名对象由 subjects 声明过滤；
    # - rankings（工具权威整体排名，集合级引用）与 group_totals（subjects/metrics/periods
    #   各自声明约束）各有权威范围，不经 input_handles 过滤。
    groups = _recompute_median_groups(facts, calc.get('input_handles'),
                                      periods_declared)
    dirs = _recompute_directions(facts)
    ranks = _recompute_ranks(facts, groups, subjects_declared)
    totals = _recompute_group_totals(block, facts, subjects_declared,
                                     metrics_declared, periods_declared)   #
    calc_missing = [str(h) for h in (calc.get('input_handles') or []) if str(h) in unresolved]
    derived_ok = bool(calc.get('input_handles')) and not calc_missing
    kind = calc.get('kind')
    if kind is not None and str(kind) not in ('median', 'direction_counts', 'rank'):
        internal_issues.append('calculation.kind 不在封闭集合：%r（不执行任意公式）' % kind)
        _add_issue(data_issues, ISSUE_CALC_INVALID)
        derived_ok = False
    if calc_missing:
        internal_issues.append('派生输入引用未核验/缺原始值：%s（该派生结果不产出）'
                               % sorted(calc_missing, key=_handle_sort_key))
        _add_issue(data_issues, _calc_unavailable_issue(kind))

    return {
        'index': index,
        'type': str(block.get('type') or ''),
        'display': {'subjects': _list_or_empty(block.get('subjects')),
                    'metrics': _list_or_empty(block.get('metrics')),
                    'periods': _list_or_empty(block.get('periods'))},
        'handle_status': handle_status,
        'verification': {
            'refs': {'total': len(handles), 'verified_n': len(facts),
                     'unresolved': sorted(unresolved, key=_handle_sort_key)},
        },
        'verified_facts': [facts[h] for h in sorted(facts, key=_handle_sort_key)],
        'recomputed': {
            'median_by_group': groups if derived_ok or not calc.get('input_handles') else [],
            # 缺输入/非法 kind 时派生通道与 median 同门槛清空：依赖缺失输入的排名/方向
            # 计数不得用剩余输入冒充完整结果；独立有效事实不受牵连。
            'direction_counts': dirs if derived_ok or not calc.get('input_handles') else {},
            'ranks': ranks if derived_ok or not calc.get('input_handles') else [],
            'tool_returned': [dict(f['tool_median'], metric=f.get('metric'),
                                   unit=f.get('unit'),
                                   product_ids=(f.get('compare_scope') or {}).get('product_ids'),
                                   period_id=(f.get('period_id')
                                              or f.get('compare_period_id')),
                                   handle=f.get('handle'), ref=f.get('ref'))
                              for f in facts.values() if f.get('tool_median')],
            # comparison 块内可比读数的服务端复算合计（无输入/口径不完整时=[]）
            'group_totals': totals,
            # **工具权威整体排名**（集合级，来自已核验的整体 compare / window_compare
            # 引用；非 `input_handles` 局部复算）。每条携带该引用的时间身份（目录周期或真实
            # 日期窗口）、指标与引用关联，供数据区完整展示原名次/值/范围。
            'rankings': _tool_rankings(facts),
        },
        'derived_value_displayable': derived_ok,
        'calculation': ({'method_text': calc.get('method'),
                         'input_handles': calc.get('input_handles') or [],
                         'missing_inputs': calc_missing,
                         'kind': kind,
                         'note': 'method 为模型自然语言文本，服务端**不执行**；'
                                 '派生值只来自封闭集合复算'}
                        if calc else None),
        'identity_warnings': identity_warnings,
        'internal_issues': internal_issues,
        'data_issues': data_issues,
        'provenance': {'run_id': str(run_id), 'evidence_channel': source.channel,
                       'refs': {h: str(resolutions.get(h) or '') for h in handles}},
    }


# 装配本次运行的展示结果。

_BODY_REF_RE = re.compile(r'\[(E\d+)\]')


def _hkey(x):
    """handle 数字序排序（E1<E2<E10），保证 ref_isolation 读数稳定。"""
    return int(str(x)[1:]) if str(x)[1:].isdigit() else 0


def _block_has_data(b: dict) -> bool:
    """块是否有可渲染数据（减法兜底：无数据且无局部说明的块不出空卡）。

    数据面＝已核验事实（cohort 块＝权威集合段）或任一确定性复算结果；只有引用/计算问题的块
    由业务面 `data_issues` 承载、仍保留，用户可见简短影响说明。"""
    if str(b.get('type') or '') == COHORT_BLOCK_TYPE:
        return b.get('cohort') is not None
    if b.get('verified_facts'):
        return True
    rec = b.get('recomputed') if isinstance(b.get('recomputed'), dict) else {}
    return bool(rec.get('median_by_group') or rec.get('direction_counts')
                or rec.get('ranks') or rec.get('tool_returned')
                or rec.get('group_totals') or rec.get('rankings'))


def build_display_result(run_id: str, intent: dict | None, *, source,
                         parse_status: str, parse_note: str | None = None,
                         body_text_for_refs: str | None = None) -> dict:
    """单 run 装配：intent（可为 None）→ 独立送检 → 逐块核验复算 → 结构化结果。

    - parse_status=failed（或 intent=None）→ 不送检、blocks=[]、degrade 登记；
    - `body_text_for_refs`：仅用于**读数对照**（正文 accepted 与 intent accepted 的
      集合关系登记），不参与任何核验判断。
    - 本函数产出**内部核验面**（含 provenance/handle_status 等审计字段，schema 字面值
      ＝RESULT_SCHEMA）；下发面必须再过 `display_blocks_wire` 的键面投影。
    """
    result = {
        'schema': RESULT_SCHEMA,
        'run_id': str(run_id),
        'evidence_channel': source.channel,
        'parse': {'status': parse_status, 'note': parse_note},
        'blocks': [],
        'degrade_reason': None,
        'ref_isolation': None,
    }
    if parse_status == STATUS_FAILED or not intent:
        result['degrade_reason'] = 'intent 解析失败：交付原始业务正文，不生成展示组件'
        return result

    blocks = intent.get('blocks') or []
    if not blocks:
        result['degrade_reason'] = None
        result['no_block_reason'] = str(intent.get('no_block_reason') or '')
        return result

    all_handles = intent_handles(intent)
    guard = source.guard(run_id, all_handles)
    accepted = [str(x) for x in (guard.get('accepted') or [])]
    resolve_resp = source.resolve(run_id, sorted(set(accepted))) if accepted else {'items': []}

    out_blocks = []
    for i, b in enumerate(blocks):
        # 全量 guard/resolve 一次即可：verify_block 按本块 handle 子集取数、逐块判局部问题
        out_blocks.append(verify_block(run_id, i, b, source, guard, resolve_resp))
    kept = [b for b in out_blocks if _block_has_data(b) or (b.get('data_issues') or [])]
    for i, b in enumerate(kept):
        b['index'] = i
    result['blocks'] = kept

    body_refs = sorted({m.group(1) for m in _BODY_REF_RE.finditer(body_text_for_refs or '')}) \
        if body_text_for_refs is not None else None
    intent_by_handle = {str(h): str(c) for h, c in (guard.get('handle_resolutions') or {}).items()}
    result['ref_isolation'] = {
        'intent_handles_n': len(all_handles),
        'intent_accepted_n': len(set(accepted)),
        'guard_rejected': guard.get('rejected') or [],
        'body_refs_n': None if body_refs is None else len(body_refs),
        'intent_only_handles': (sorted(set(intent_by_handle) - set(body_refs), key=_hkey)
                                if body_refs is not None else None),
        'note': '展示 accepted 不授予正文引用资格；正文 Guard 链独立执行、互不冒充',
    }
    return result


def to_jsonl_safe(obj) -> str:
    """落盘前兜底：非 JSON 原生值（float('inf') 等合同面不存在，防万一）如实转字符串。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---------- 5. 服务层入口（server.py run 流收尾调用；失败一律 None，正文交付不受影响） ----------

#  确定性兜底：模型自己不产 display_blocks、或产了但完全没有 group_totals 时，用 Data 已在
# `period_diff_set.summary.category_totals` 里算好的多商品品类聚合兜底出 comparison 块。
# 不新建第二套展示 schema、不解析 Markdown——仍走同一份 `display_blocks_wire` 键面，前端复用
# 同一个 GroupTotalsPanel。品类只来自 Data 的目录口径（不硬编码任何品类名）。

def _fallback_group_total(cat: dict, metric: str, unit, base_period_id: str,
                          target_period_id: str) -> dict | None:
    """Data 品类合计条目 → Wire `group_totals` 一条（数值直接映射，不复算、不代 0）。"""
    if not isinstance(cat, dict):
        return None
    coverage = cat.get('coverage') if isinstance(cat.get('coverage'), dict) else {}
    percent = cat.get('percent_change')
    tokens = unknown_reason_tokens(percent)      # 'unknown(zero_denominator)' 等 → 状态码
    if tokens:
        percent_state, percent_value = tokens[0], None
    elif isinstance(percent, (int, float)) and not isinstance(percent, bool):
        percent_state, percent_value = 'ok', percent
    else:
        percent_state, percent_value = None, None
    by_state = coverage.get('non_comparable_by_state')
    return {
        'category': cat.get('category'),
        'metric': metric,
        'unit': unit,
        'base_period_id': base_period_id,
        'target_period_id': target_period_id,
        'subjects_n': coverage.get('selected_product_count'),
        'comparable_n': coverage.get('comparable_product_count'),
        'non_comparable_n': coverage.get('non_comparable_product_count'),
        'non_comparable_by_state': (
            {str(k): int(v) for k, v in by_state.items()
             if isinstance(v, int) and not isinstance(v, bool)}
            if isinstance(by_state, dict) else {}),
        'base_sum': cat.get('base_sum'),
        'target_sum': cat.get('target_sum'),
        'absolute_change': cat.get('absolute_change'),
        'percent_change': percent_value,
        'percent_change_state': percent_state,
        'sum_scope': str(cat.get('sum_scope') or 'comparable_products_only'),
        'basis': 'tool_returned',
    }


def _value_pair_snapshots(snapshots) -> list[dict]:
    """value 路径快照 → diff 形伪快照：把**商品集合完全一致**的基期/对比期紧凑周期值
    配对，用 Data 的 `category_totals_from_value_pairs` 合成等价品类合计。

    覆盖「模型走 catalog → value×2 → states → knowledge 而不用 diff」的路径——此时 Data 没有
    现成 `period_diff_set`，品类合计由 Data 同一判据在基期/对比期 value 值上等价给出。
    三条确定性边界（宁可不出卡，不猜）：
    - 两期商品集合不一致（非同一笔比较）→ 不配对；
    - 该商品集合必须被 states 身份快照**完整覆盖**（每商品都有 identity_state）——缺 states
      或覆盖不完整时无法复现 diff 的身份门禁，不生成 fallback；
    - 基期/对比期按目录日期序确定（早者=基期，差值=对比期−基期，与 diff 口径一致）；配对后
      身份不一致商品按不可比处理、不代 0。"""
    identity_states: dict[str, str] = {}
    for snap in (snapshots or []):
        if isinstance(snap, dict) and snap.get('kind') == 'states':
            for pid, row in (snap.get('rows') or {}).items():
                if isinstance(row, dict) and row.get('identity_state'):
                    identity_states[str(pid)] = str(row['identity_state'])
    by_metric: dict[str, list] = {}
    for snap in (snapshots or []):
        if isinstance(snap, dict) and snap.get('kind') == 'values':
            by_metric.setdefault(str(snap.get('metric') or ''), []).append(snap)
    out: list[dict] = []
    for metric, snaps in by_metric.items():
        if metric not in ('M1', 'M2'):
            continue
        for i in range(len(snaps)):
            for j in range(i + 1, len(snaps)):
                a, b = snaps[i], snaps[j]
                if str(a.get('period_id') or '') == str(b.get('period_id') or ''):
                    continue
                base_ids = a.get('product_ids')
                target_ids = b.get('product_ids')
                if not isinstance(base_ids, list) or not isinstance(target_ids, list):
                    continue      # 缺商品集合（旧形状）不配对
                if base_ids != target_ids:
                    continue      # 两期商品集合不一致＝不是同一笔比较，不取交集、不猜
                base_snap, target_snap = sorted(
                    (a, b), key=lambda s: _period_sort_key(s.get('period_id')))
                base_period = str(base_snap.get('period_id') or '')
                target_period = str(target_snap.get('period_id') or '')
                paired = list(base_ids)
                if len(paired) < 2:
                    continue
                # states 身份覆盖门（fail-closed）：集合内每商品都须有 identity_state
                if any(str(pid) not in identity_states for pid in paired):
                    continue
                base_rows = [dict({'product_id': pid}, **row)
                             for pid, row in base_snap['rows'].items() if pid in paired]
                target_rows = [dict({'product_id': pid}, **row)
                               for pid, row in target_snap['rows'].items() if pid in paired]
                # 身份不一致门禁（与 diff 同源）：该商品按不可比处理、不代 0（period_values
                # 读数是单期的，跨期身份只能由 states 快照给出）
                for row in base_rows + target_rows:
                    if identity_states.get(str(row['product_id'])) == 'identity_inconsistent':
                        row.update(value=None, state='identity_inconsistent')
                totals = category_totals_from_value_pairs(base_rows, target_rows, metric, PRODUCTS)
                if not isinstance(totals, dict) or not (totals.get('categories') or []):
                    continue
                units = {u for s in (base_snap, target_snap)
                         if s.get('unit') for u in [s['unit']]}
                out.append({'kind': 'diff', 'product_ids': paired,
                            'metric': metric, 'base_period_id': base_period,
                            'target_period_id': target_period,
                            'unit': (next(iter(units)) if len(units) == 1 else None),
                            'category_totals': totals})
    return out


def _is_category_analysis_question(user_question: str) -> bool:
    """「这次结果是不是品类分析」的确定性判据（兜底触发闸门）：问题里点名了目录品类名或
    出现「品类」字眼才允许品类合计兜底，普通多商品比较题不产「品类两期对比」卡。

    词面来自目录单点（`operating_catalog.PRODUCTS` 的 category），不解析模型回答 Markdown；
    匹配不到＝保守不加卡（宁可少加，不给用户没要求的汇总卡）。"""
    question = str(user_question or '')
    if '品类' in question:
        return True
    return any(str(cat) and str(cat) in question
               for cat in sorted({str(p.get('category')) for p in PRODUCTS
                                  if isinstance(p.get('category'), str) and p['category']}))


def _period_sort_key(period_id: Any):
    """周期时间序键（目录日期序；日期缺失/不可识别的排最后，兜底输入不炸）。"""
    p = period_by_id(str(period_id or '')) or {}
    return (str(p.get('period_start_date') or ''), str(period_id or ''))


def category_total_fallback_blocks(snapshots, run_id: str,
                                   start_index: int = 0,
                                   user_question: str = '') -> list[dict]:
    """运行期品类合计快照 → 现有 `oagent_display_blocks_v1` 的 comparison 块（确定性兜底）。

    只针对**同一商品集合 + 同一周期对**的实际多商品可加总聚合结果：
    - diff 路径：快照来自 `RunContext.note_category_totals`（Data 已算好的
      `summary.category_totals`）；
    - value 路径：快照来自 `RunContext.note_period_values`，经 `_value_pair_snapshots`
      配对合成（覆盖模型走 value×2 而不用 diff）。
    每组合成一个 comparison 块，只填前端展示品类合计必须的数据：type/display（subjects/
    metrics/periods）/recomputed.group_totals；其余走既有合法空值。产出仍过同一份
    `display_blocks_wire` 键面投影（键面收口不另开一套）；无可用快照返回 []。

    `user_question`＝**兜底触发闸门**：点了品类（目录品类名或「品类」字眼）才产品类合计；
    服务层（server.run）恒传真实入口问题。缺省空串＝不设闸门（供测试与旧调用方；正式链路
    不依赖该缺省）。"""
    if user_question and not _is_category_analysis_question(user_question):
        return []
    diff_like = _value_pair_snapshots(snapshots) + [
        s for s in (snapshots or []) if isinstance(s, dict) and s.get('kind') != 'values']
    groups: list[dict] = []
    index_of: dict = {}
    for snap in diff_like:
        totals = snap.get('category_totals')
        categories = totals.get('categories') if isinstance(totals, dict) else None
        product_ids = snap.get('product_ids')
        base = str(snap.get('base_period_id') or '')
        target = str(snap.get('target_period_id') or '')
        metric = str(snap.get('metric') or '')
        if (not isinstance(categories, list) or not categories
                or not isinstance(product_ids, list) or len(product_ids) < 2
                or not base or not target or not metric):
            continue
        key = (tuple(product_ids), base, target)
        if key not in index_of:
            index_of[key] = len(groups)
            groups.append({'product_ids': list(product_ids), 'base': base, 'target': target,
                           'metrics': [], 'totals': []})
        group = groups[index_of[key]]
        if metric not in group['metrics']:
            group['metrics'].append(metric)
        seen_totals = {(t['metric'], t['category']) for t in group['totals']}
        for cat in categories:
            entry = _fallback_group_total(cat, metric, snap.get('unit'), base, target)
            if entry is not None and (metric, entry['category']) not in seen_totals:
                seen_totals.add((metric, entry['category']))
                group['totals'].append(entry)

    blocks: list[dict] = []
    for group in groups:
        if not group['totals']:
            continue
        blocks.append({
            # 序号按实际产块回收（跳过无 totals 的组不留空洞）
            'index': int(start_index) + len(blocks),
            'type': 'comparison',
            'display': {'subjects': sorted(str(p) for p in group['product_ids']),
                        'metrics': list(group['metrics']),
                        'periods': [group['base'], group['target']]},
            'verified_facts': [],
            'recomputed': {'median_by_group': [], 'direction_counts': {},
                           'ranks': [], 'tool_returned': [],
                           'group_totals': group['totals']},
            'verification': {'refs': {'total': 0, 'verified_n': 0, 'unresolved': []}},
            'data_issues': [],
            'cohort': None,
        })
    if not blocks:
        return []
    wire = display_blocks_wire({'schema': RESULT_SCHEMA, 'run_id': str(run_id or ''),
                               'parse': {'status': 'deterministic_fallback'},
                               'blocks': blocks})
    return (wire or {}).get('blocks') or []


def build_display_blocks(*, client, run_id: str, final_text: str) -> dict | None:
    """原始终答 → 解析 → 同 run 独立送检/回查 → 复算 → Wire 投影（`oagent_display_blocks_v1`）。

    无标记／解析失败／`blocks=[]`／通道不可用（账本失效、Guard/resolve 异常）／任何未预期
    异常 = 返回 None：正文、`report`、`evidence_details`、`delivery_state` 与六类事件键面
    均不受影响（步骤 5 的失败降级口径；调用方另以超时兜底）。
    """
    try:
        parsed = parse_display_intent(str(final_text or ''))
        if parsed['status'] == STATUS_FAILED or not parsed.get('intent'):
            return None
        if not (parsed['intent'].get('blocks') or []):
            return None
        result = build_display_result(
            run_id, parsed['intent'], source=ClientRunSource(client),
            parse_status=parsed['status'], parse_note=parsed['note'],
            body_text_for_refs=parsed['business_text'])
        # 内部技术成因（未签发句柄/合同字段/计算实现）只写诊断日志，不下发业务面。
        for b in (result.get('blocks') or []):
            if isinstance(b, dict) and b.get('internal_issues'):
                log.info('run %s display block %s internal issues: %s',
                         run_id, b.get('index'), b['internal_issues'])
        return display_blocks_wire(result)
    except SourceUnavailable:
        return None
    except Exception as exc:                        # noqa: BLE001  展示链异常不得影响正文交付
        log.warning('run %s display_blocks 装配异常，降级为 null：%s: %s',
                    run_id, type(exc).__name__, exc)
        return None
