# -*- coding: utf-8 -*-
"""Root Agent 的工具执行、预算校准、证据登记与模型可见投影。

四个 MCP 查询工具由 SDK 会话发现和执行，schema 来自 mcp_query_tools.contract。
输入与输出守卫复用 oagent_query 的查询前后处理；oagent_query 仅开放 states/daily_series。
build_tools 返回 oagent_query 与 knowledge_search，工具描述来自 core.prompts。
参数拒绝不扣预算；传输和业务失败明确回传。知识检索不占查询预算，重复查询受收敛闸限制。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import time
from typing import Any, Callable

from agents import RunContextWrapper, function_tool
from agents.exceptions import ModelBehaviorError
from agents.run_config import ToolErrorFormatterArgs
from agents.tool import FunctionTool
from agents.tool_guardrails import (ToolGuardrailFunctionOutput, ToolInputGuardrail,
                                    ToolInputGuardrailData, ToolOutputGuardrail,
                                    ToolOutputGuardrailData)
from core.prompts import CURRENT_OPENAI_TOOLS
from core.root_contract import (KNOWLEDGE_DELIVERY_MODE,
                                            knowledge_search_response,
                                            tool_call_status)
from core.tool_client import tool_response_text
# 品类合计引用键的品类身份段（`category_total:<metric>:<category>` 的 `<category>`）
# 取自签发侧同一函数，不在本模块手写第二份归一规则（键形与回查必须同源）。
from core.data import pctx_query as _PQ

from .context import RunContext
from .mcp_query_tools import contract as mcp_contract

_FROZEN: dict[str, dict] = {t['function']['name']: t['function'] for t in CURRENT_OPENAI_TOOLS}
# MCP 工具定义由会话从 Server 发现；按合同工具映射生成操作到工具名的逆表。
_MIGRATED_OP_TOOL: dict[str, str] = {op: name for name, op in mcp_contract.TOOL_OPS.items()}
# 内部专用 op——`catalog` 不再是模型可选操作（可信目录只作内部范围解析与运行期上下文
# 用途）。模型要目录内容用任务输入里已下发的商品目录，或各查询工具的 `scope`。
_INTERNAL_ONLY_OPS = ('catalog',)

# 知识循环收口：连续 _KNOWLEDGE_CLOSE_STREAK 次执行「零新增内容」后，本 run 关闭
# knowledge_search（闸-拒绝同形，不占预算语义不变）。检索面为确定性本地索引 ⇒ 完全重复的
# (query, doc_type) 必然返回完全相同结果，拒绝执行不损失任何信息。
_KNOWLEDGE_CLOSE_STREAK = 2


def _knowledge_loop_reject(error_code: str, detail: str) -> str:
    return tool_response_text({'ok': False, 'error_code': error_code, 'detail': detail})


def _knowledge_item_key(result: dict) -> tuple[str, str, str]:
    """单条检索结果的记账键：(doc_id, section, 正文内容摘要)。

    粒度取到正文：同一节的另一段正文（内容不同）算不同条目——换角度检索取回同节的
    新正文仍是新增内容，不推进零新增计数；同节同正文再次出现才算已见。确定性本地索引
    ⇒ 同 chunk 内容逐字稳定，内容摘要可作稳定标识；chunk 语义 id 不进模型上下文
    （合同），响应面只有 content 可用。
    """
    digest = hashlib.sha256(str(result.get('content') or '').encode('utf-8')).hexdigest()[:16]
    return (str(result.get('doc_id') or ''), str(result.get('section') or ''), digest)


def _knowledge_note(new_n: int, total_n: int, closed_now: bool) -> str:
    """模型可见的检索增量注记（确定性事实，不猜原因、不写 Case 专属内容）。"""
    note = '本次返回 %d 条，其中新增 %d 条' % (total_n, new_n)
    if new_n < total_n:
        note += '（其余 %d 条在本 run 前序检索中已出现过）' % (total_n - new_n)
    note += '。'
    if closed_now:
        note += ('连续 %d 次检索均未返回新内容：本 run 的知识检索已关闭，后续调用将被拒绝；'
                 '请基于已有信息直接输出最终回答。' % _KNOWLEDGE_CLOSE_STREAK)
    elif new_n == 0:
        note += '本次检索没有产生任何新内容；请勿以同一思路继续重复检索。'
    return note


def _fold_diff_rows(rows, handle_by_ref, row_refs):
    """把逐商品 diff 行折叠成「共享区 + 紧凑行」（单指标与多指标共用同一实现）。

    返回 dict：`shared_row` / `period_identity_fields` / `shared_pair` / `shared_common` /
    `shared_base` / `shared_target` / `rows`。折叠判据（≥2 持有者 + 多数行 + 取值全等）、
    absent 标注与逐字段可逆性在两条路径上完全一致——不复制第二份压缩规则。
    """
    n_rows = len(rows)

    def _lift(holders):
        """holders: {key: [values]} → 上提键 {key: value}（≥2 拥有者 + 多数行 + 全等）。"""
        return {key: values[0] for key, values in holders.items()
                if len(values) >= 2 and len(values) > n_rows / 2 and _same_value(values)}

    row_holders = {}
    for row in rows:
        for key in row:
            if key not in ('base', 'target'):
                row_holders.setdefault(key, []).append(row[key])
    shared_row = _lift(row_holders)

    paired = [row for row in rows if isinstance(row.get('base'), dict)
              and isinstance(row.get('target'), dict)]
    period_identity_fields = [
        key for key in ('product_id',)
        if paired and all(key in row['base'] and key in row['target']
                          and row['base'][key] == row['target'][key]
                          and (key != 'product_id' or row['base'][key] == row.get('product_id'))
                          for row in paired)
    ]
    pair_keys = (set.intersection(*(set(row['base']) & set(row['target']) for row in paired))
                 if paired else set())
    pair_values = {
        key: [row['base'][key] for row in paired]
        for key in pair_keys
        if all(row['base'][key] == row['target'][key] for row in paired)
    }
    shared_pair = {key: values[0] for key, values in pair_values.items()
                   if all(value == values[0] for value in values)}

    # 批 1 新增顶层块 shared_common：period_common 候选（行内 base==target 同值、
    # 非 product_id、非 shared_pair）里跨行同值 + 多数行的键，上提到顶层共享块。
    common_holders = {}
    for row in paired:
        base, target = row['base'], row['target']
        for key in set(base) & set(target):
            if key != 'product_id' and key not in shared_pair and base[key] == target[key]:
                common_holders.setdefault(key, []).append(base[key])
    shared_common = _lift(common_holders)

    # shared_sides：判据同 _lift；排除所有行内同值键（shared_pair ∪ period_common
    # 候选）——它们走 shared_pair / shared_common / period_common 通道，不从该侧块上提。
    side_excluded = set(shared_pair) | set(common_holders)
    shared_sides = {}
    for side in ('base', 'target'):
        present = [row[side] for row in rows if isinstance(row.get(side), dict)]
        holders = {}
        for block in present:
            for key in block:
                holders.setdefault(key, []).append(block[key])
        shared_sides[side] = {key: value for key, value in _lift(holders).items()
                              if key not in side_excluded}

    packed = []
    for row, canonical_ref in zip(rows, row_refs):
        item = {key: value for key, value in row.items()
                if key not in shared_row and key not in ('base', 'target')}
        item['ref'] = handle_by_ref[canonical_ref]
        item['pair_shared'] = isinstance(row.get('base'), dict) and isinstance(row.get('target'), dict)
        # 行级 absent：shared_row 键中该行确实没有的（仅非空时出现）。
        absent = [key for key in sorted(shared_row) if key not in row]
        if absent:
            item['absent'] = absent
        common = {}
        if item['pair_shared']:
            base, target = row['base'], row['target']
            common = {
                key: base[key] for key in sorted(set(base) & set(target))
                if key not in shared_pair and key not in shared_common
                and base[key] == target[key] and key != 'product_id'
            }
            if common:
                item['period_common'] = common
                # absent_common：shared_common 键中该行 base/target 任一侧确实没有的
                # （缺键；复原时这些键不注回，行内有值的一侧以行内原值为准）。
                absent_common = [key for key in sorted(shared_common)
                                 if key not in base or key not in target]
                if absent_common:
                    item['absent_common'] = absent_common
        for side in ('base', 'target'):
            if isinstance(row.get(side), dict):
                block = row[side]
                item[side] = {
                    key: value for key, value in block.items()
                    if not (item['pair_shared'] and key in shared_pair)
                    and key not in shared_sides[side]
                    # shared_common 键从该侧块剔除：仅当该键在本行也是 period_common
                    # 候选（base 与 target 都有且行内同值）——非候选键（一侧缺键、或
                    # 行内值不同）保留行内原值，复原时由行内值覆盖注回值。
                    and not (item['pair_shared'] and key in shared_common
                             and key in base and key in target
                             and base[key] == target[key])
                    and not (key in common and block[key] == common[key])
                    and key not in period_identity_fields
                }
                # side absent：shared_sides[side] 键中该 side 块确实没有的（仅非空时出现）。
                absent_side = [key for key in sorted(shared_sides[side]) if key not in block]
                if absent_side:
                    item['absent_' + side] = absent_side
        packed.append(item)
    return {
        'shared_row': {key: shared_row[key] for key in sorted(shared_row)},
        'period_identity_fields': period_identity_fields,
        'shared_pair': {key: shared_pair[key] for key in sorted(shared_pair)},
        'shared_common': {key: shared_common[key] for key in sorted(shared_common)},
        'shared_base': {key: shared_sides['base'][key] for key in sorted(shared_sides['base'])},
        'shared_target': {key: shared_sides['target'][key] for key in sorted(shared_sides['target'])},
        'rows': packed,
    }


def _project_period_diff_set(response: dict, query: dict) -> dict:
    """压缩多商品跨期比较的模型可见响应，原始响应仍用于证据与运行登记。

保留各指标状态、差值、句柄、异常及品类合计，重复的公共字段提升到共享位置。"""
    product_ids = query.get('product_ids')
    event = response.get('event')
    # 日期窗口 diff（period_window_diff_set）行形状与周期 diff 同构（逐行重复的指标语义
    # + base/target 两侧块），只是行引用键形不同（window_diff:...）、周期身份换成真实起止日期；
    # 复用同一折叠机制，按事件取不同的行引用键形与前缀。形状不符一律原样返回。
    if event == 'period_window_diff_set':
        def _row_ref(row):
            return 'window_diff:%s:%s:%s' % (row.get('product_id'), row.get('metric'),
                                             row.get('state'))
    elif event == 'period_diff_set':
        def _row_ref(row):
            return 'diff:%s:%s:%s' % (row.get('product_id'), row.get('metric'), row.get('state'))
    else:
        return response
    if query.get('op') != 'diff':
        return response

    n_members = 0
    if isinstance(product_ids, list):
        n_members = len(product_ids)
    else:
        # 范围成员优先使用服务端 scope_resolution.members 回显。
        resolution = response.get('scope_resolution')
        if isinstance(resolution, dict) and isinstance(resolution.get('members'), list):
            n_members = len(resolution['members'])
    if n_members < 2:
        return response

    by_metric = response.get('by_metric')
    if isinstance(by_metric, dict) and by_metric:
        return _project_period_diff_set_multi(response, query, by_metric, _row_ref)

    metric = query.get('metric')
    if metric is None:
        # `metrics[]` 只有 1 项时数据层仍走单指标形态（响应带顶层 `metric`），
        # 请求侧按同一指标对齐后照常投影。
        requested_metrics = query.get('metrics')
        if isinstance(requested_metrics, list) and len(requested_metrics) == 1:
            metric = requested_metrics[0]
    if response.get('metric') != metric:
        return response

    rows = response.get('results')
    refs = response.get('qev')
    handles = response.get('evidence_handles')
    if (not isinstance(rows, list) or len(rows) < 2
            or not isinstance(refs, list) or not isinstance(handles, dict)
            or any(not isinstance(row, dict) for row in rows)
            or any(not isinstance(ref, str) for ref in refs)
            or any(not isinstance(handle, str) or not isinstance(canonical, str)
                   for handle, canonical in handles.items())):
        return response

    handle_by_ref = {canonical: handle for handle, canonical in handles.items()}
    if len(handle_by_ref) != len(handles):
        return response
    if set(handle_by_ref) != set(refs):
        return response                      # handles↔qev 值集不一致=不投影（fail-closed）
    qid = str(response.get('query_id') or '')
    if not qid:
        return response
    # 行↔行引用对应：按确定性键形逐行还原 canonical，不依赖列表顺序；每行引用都必须
    # 已登记（证据覆盖全部返回行），行引用之外只允许品类汇总引用。
    aggregate_prefix = 'category_total:'
    row_refs = []
    for row in rows:
        canonical = 'pqev:%s:%s' % (qid, _row_ref(row))
        row_refs.append(canonical)
    row_ref_set = set(row_refs)
    if not row_ref_set <= set(refs):
        return response
    aggregate_refs = [r for r in refs
                      if r not in row_ref_set
                      and r.startswith('pqev:%s:%s' % (qid, aggregate_prefix))]
    if len(aggregate_refs) != len(set(refs) - row_ref_set):
        return response                      # 行引用与聚合引用之外的形状=不投影（fail-closed）
    if any(row.get('product_id') is None for row in rows):
        return response

    # 模型投影移除版本与溯源元数据；权威原件保留在服务端登记中。
    projected = {key: value for key, value in response.items()
                 if key not in ('results', 'qev', 'provenance', 'entry_contract_version',
                                'contract_version', 'view_version', 'generator_version', 'seed')}
    projected.update(_fold_diff_rows(rows, handle_by_ref, row_refs))
    # 品类汇总引用显式暴露（handle→品类名）——每条品类合计都有独立 handle，模型可
    # 直接对汇总值标 [E#]；canonical 全量映射仍随 evidence_handles 下发。
    projected['category_total_handles'] = {
        handle_by_ref[ref]: ref[len('pqev:%s:%s' % (qid, aggregate_prefix)):]
        for ref in aggregate_refs if ref in handle_by_ref
    }
    projected['projection_note'] = (
        'period_diff_set 模型投影：逐商品 diff 行的重复字段已折叠到共享区（shared_*，'
        'period_common 内跨行同值字段上提到 shared_common），行级只留差异与证据 handle；'
        'absent / absent_base / absent_target / absent_common 列出该行确实不存在的共享区键，'
        '未列出的共享区键按共享区取值理解；qev 与 evidence_handles 值集可证等价时不再重复下发；'
        'summary.category_totals 的每条品类汇总各有独立 handle（见 category_total_handles，'
        '数值/覆盖/不可比分类在 summary 内原样保留），可对其直接标注 [E#]；'
        '商品与状态事实均未截断，原始响应与证据回查不受影响')
    return projected


def _project_period_diff_set_multi(response, query, by_metric, row_ref):
    """多指标 diff（`by_metric` 信封）模型可见投影——逐指标同一套折叠机制。

    与单指标路径的差别只在「按指标分块」：每个指标块内 shared_* / rows 的折叠判据与
    可逆性完全一致（共用 `_fold_diff_rows`），**指标之间不合并**：不同指标的 handle、
    状态与不可比原因不混成一个结论。集合级引用按指标维度分别暴露
    （`category_total_handles` 值＝{metric, category}），与单指标的 handle→品类串不同形，
    以免模型把不同指标的合计当成同一笔。
    """
    refs = response.get('qev')
    handles = response.get('evidence_handles')
    qid = str(response.get('query_id') or '')
    if (not isinstance(refs, list) or not isinstance(handles, dict) or not qid
            or any(not isinstance(ref, str) for ref in refs)
            or any(not isinstance(handle, str) or not isinstance(canonical, str)
                   for handle, canonical in handles.items())):
        return response
    handle_by_ref = {canonical: handle for handle, canonical in handles.items()}
    if len(handle_by_ref) != len(handles) or set(handle_by_ref) != set(refs):
        return response                      # handles↔qev 值集不一致=不投影（fail-closed）

    prefix = 'pqev:%s:' % qid
    aggregate_prefix = prefix + 'category_total:'
    blocks = {}
    row_ref_set = set()
    for metric, block in by_metric.items():
        if not isinstance(block, dict):
            return response
        rows = block.get('results')
        if (not isinstance(rows, list) or len(rows) < 1
                or any(not isinstance(row, dict) for row in rows)
                or any(row.get('product_id') is None for row in rows)):
            return response
        row_refs = [prefix + row_ref(row) for row in rows]
        if not set(row_refs) <= set(refs):
            return response
        row_ref_set |= set(row_refs)
        blocks[metric] = (block, rows, row_refs)
    # 行引用之外的引用必须全部是该响应的品类合计引用（fail-closed）。
    aggregate_refs = [r for r in refs if r not in row_ref_set]
    if any(not r.startswith(aggregate_prefix) for r in aggregate_refs):
        return response
    # 多指标形态的品类合计键带指标段（`category_total:<metric>:<category>`）：按指标归组，
    # 避免把不同指标的同品类合计混为一笔。
    aggregate_by_metric = {}
    for ref in aggregate_refs:
        head, sep, _tail = ref[len(aggregate_prefix):].partition(':')
        if not sep:
            return response
        aggregate_by_metric.setdefault(head, []).append(ref)

    projected_metrics = {}
    for metric, (block, rows, row_refs) in blocks.items():
        summary = block.get('summary') if isinstance(block.get('summary'), dict) else {}
        totals = summary.get('category_totals') if isinstance(summary.get('category_totals'), dict) else {}
        expected = []
        for entry in (totals.get('categories') or []):
            if not isinstance(entry, dict):
                return response
            expected.append('%s%s:%s' % (aggregate_prefix, metric,
                                         _PQ.category_total_ref_key(entry.get('category'))))
        owned = aggregate_by_metric.get(metric, [])
        if sorted(expected) != sorted(owned):
            return response                      # 品类合计引用与响应内实际条目不一一对应
        metric_projected = {key: value for key, value in block.items() if key != 'results'}
        metric_projected.update(_fold_diff_rows(rows, handle_by_ref, row_refs))
        metric_projected['category_total_handles'] = {
            handle_by_ref[ref]: {'metric': metric,
                                 'category': ref[len(aggregate_prefix) + len(metric) + 1:]}
            for ref in owned if ref in handle_by_ref
        }
        metric_projected['projection_note'] = (
            '多指标跨期比较（by_metric）模型投影：本指标块的逐商品 diff 行重复字段已折叠到'
            '共享区（shared_*），行级只留差异与证据 handle；absent* 列出该行确实不存在的'
            '共享区键；本块的品类汇总各有独立 handle（见 category_total_handles，值含 '
            'metric 与 category），可对其直接标注 [E#]；指标之间不合并、不共享「整体可比」'
            '判断，某指标不可比不影响其他指标；原始响应与证据回查不受影响')
        projected_metrics[metric] = metric_projected

    projected = {key: value for key, value in response.items()
                 if key not in ('by_metric', 'qev', 'provenance', 'entry_contract_version',
                                'contract_version', 'view_version', 'generator_version', 'seed')}
    projected['by_metric'] = projected_metrics
    projected['projection_note'] = (
        'period_diff_set 多指标模型投影：本次请求了 %s 指标，响应按 by_metric 逐指标分块；'
        '每块内逐商品 diff 行的重复字段已折叠到共享区（shared_*），行级只留差异与证据 '
        'handle；各指标的 handle、状态与不可比原因互不合并——某指标 unknown/不可比不牵连'
        '其他指标；集合级引用按指标维度分别暴露（见各块 category_total_handles）；'
        'qev 与 evidence_handles 值集可证等价时不再重复下发；原始响应与证据回查不受影响'
        % '/'.join(response.get('metrics_requested') or list(projected_metrics)))
    return projected


_VALUE_METRIC_DEF_KEYS = ('metric_name', 'unit', 'role', 'business_layer',
                          'aggregation_population', 'inference_limits', 'source_layer',
                          'source_field_or_formula', 'formula', 'missing_rule')
_VALUE_PERIOD_KEYS = ('period_id', 'period_label', 'period_start_date', 'period_end_date',
                      'period_length_days', 'window_note', 'demo_caliber', 'period_meta_echo')


def _same_value(values: list) -> bool:
    """一组值是否逐字相同（JSON 语义比较；用于「每行重复字段」判定，不看对象身份）。"""
    return len({json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
                for v in values}) == 1


def _handle_lookup(response: dict) -> tuple[str, dict]:
    """响应 → (query_id, canonical→handle)。handles 形状不符时返回空映射（调用方原样返回）。"""
    handles = response.get('evidence_handles')
    if not isinstance(handles, dict):
        return '', {}
    by_canon = {str(c): str(h) for h, c in handles.items()
                if isinstance(h, str) and isinstance(c, str)}
    return str(response.get('query_id') or ''), by_canon


def _qev_redundant(response: dict) -> bool:
    """`qev` 是否可证等价于 `evidence_handles` 的 canonical 值集（可安全省略）。

    模型只按 [E#] handle 引用，canonical 由 handle→canonical 映射（仍全量下发）唯一确定；
    仅当两份值集逐项相等时才认定 `qev` 为纯重复副本。形状不符/缺失/不等价一律返回 False
    （fail-closed，调用方保留原件）。与 `_project_period_set_compare` 同一判据，并让
    value / states 与 diff / set_compare 四个投影的 qev 口径一致。
    """
    handles = response.get('evidence_handles')
    qev = response.get('qev')
    if (not isinstance(handles, dict) or not isinstance(qev, list)
            or any(not isinstance(h, str) or not isinstance(c, str)
                   for h, c in handles.items())):
        return False
    return sorted(handles.values()) == sorted(str(ref) for ref in qev)


def _project_scope_resolution(resolution):
    """共享业务范围解析回显的**模型可见**投影（单点；`value` 与 `cohort_filter` 共用）。

    模型只需范围/覆盖/上界，**不需要成员长名单**——成员由逐行结果给出，品类成员不由
    模型枚举（目录随任务输入下发，重复下发 60 个 ID 只是体量浪费）。因此这里只留
    `requested` / `resolved_kind` / 品类摘要 / `member_count` / `bound` / `coverage` /
    `order`，成员清单（`members` 与 `categories[].members`）不下发。

    两条投影路径不各裁一份形状——否则同一份 `scope_resolution` 在 `value` 与筛选面上
    会长得不一样，下游消费面无法用同一套读法。
    """
    if not isinstance(resolution, dict):
        return None
    return {
        'requested': resolution.get('requested'),
        'resolved_kind': resolution.get('resolved_kind'),
        'categories': [{'category': c.get('category'),
                        'catalog_member_count': c.get('catalog_member_count'),
                        'member_count': c.get('member_count'),
                        'coverage_complete': c.get('coverage_complete')}
                       for c in (resolution.get('categories') or [])
                       if isinstance(c, dict)],
        'member_count': resolution.get('member_count'),
        'bound': resolution.get('bound'),
        'coverage': resolution.get('coverage'),
        'order': resolution.get('order'),
    }


def _project_period_values(response: dict, query: dict) -> dict:
    """period_values 模型可见投影：只压缩多商品成功响应，单商品与异常响应原样返回。

    每行重复的字段（period_id、指标定义/单位/业务语义/口径、周期窗口与 Demo 口径）提到共享区
    只出现一次；每行只保留回答所需：product_id、metric、value、state/unknown_reason、
    –判断所需的分母与计算字段、对应证据 handle。period_missing / field_missing /
    record_conflict / zero_denominator 等状态与 unknown 原样保留，不截断商品、不代 0。
    原始服务端响应与证据回查（evidence_handles 指向的登记）不受影响。
    """
    rows = response.get('results')
    event = response.get('event')
    if query.get('op') != 'value' or event not in ('period_values', 'period_window_values'):
        return response
    # 日期窗口 value（period_window_values）行形状与周期 value 同构（逐行重复的指标语义
    # ＋真实起止日期），只是行引用键形是 window_object:<pid>:<metric>、无 period_id。复用同一
    # 折叠机制，按事件取不同的行引用键形与前缀。
    window_event = event == 'period_window_values'
    requested = query.get('product_ids')
    n_products = len(requested) if isinstance(requested, list) else len(response.get('objects') or [])
    # 带品类合计或贡献的响应即使只有单商品也要投影，保留模型需要的合计与分母引用。
    has_category_totals = (isinstance(response.get('category_totals'), dict)
                           and bool(response.get('category_totals')))
    if not isinstance(rows, list) or not rows \
            or any(not isinstance(row, dict) for row in rows):
        return response
    if not has_category_totals and (n_products < 2 or len(rows) < 2):
        return response
    qid, handle_by_canon = _handle_lookup(response)
    if not handle_by_canon:
        return response

    metric_defs: dict[str, dict] = {}
    for row in rows:
        metric = str(row.get('metric') or '')
        if metric and metric not in metric_defs:
            metric_defs[metric] = {k: row[k] for k in _VALUE_METRIC_DEF_KEYS if k in row}
    period_shared: dict = {}
    for key in _VALUE_PERIOD_KEYS:
        present = [row[key] for row in rows if key in row]
        if present and _same_value(present):
            period_shared[key] = present[0]

    product_names: dict = {}
    dropped = set(_VALUE_METRIC_DEF_KEYS) | set(period_shared)
    packed = []
    for row in rows:
        product_id = str(row.get('product_id') or '')
        if row.get('product_name') is not None and product_id not in product_names:
            product_names[product_id] = row.get('product_name')
        item = {key: value for key, value in row.items()
                if key not in dropped and key != 'product_name'}
        contribution = row.get('contribution')
        if isinstance(contribution, dict):
            # 贡献度随行下发；分母 canonical 引用换成同一 handle（不新造第二套编号）。
            contribution = dict(contribution)
            denominator_handle = handle_by_canon.get(
                str(contribution.get('denominator_ref') or ''))
            if denominator_handle:
                contribution['denominator_ref'] = denominator_handle
            item['contribution'] = contribution
        if window_event:
            canonical = 'window_object:%s:%s' % (product_id, row.get('metric'))
        else:
            canonical = ('object:%s:%s:%s' % (product_id, row.get('period_id'), row.get('metric'))
                         if row.get('state') == 'ok'
                         else 'state:%s:%s' % (product_id, row.get('period_id')))
        handle = handle_by_canon.get('pqev:%s:%s' % (qid, canonical))
        if handle:
            item['ref'] = handle
        packed.append(item)

    projected = {key: value for key, value in response.items()
                 if key not in ('results', 'provenance', 'entry_contract_version',
                                'contract_version', 'view_version', 'generator_version', 'seed')}
    projected['metric_defs'] = metric_defs
    projected['period_shared'] = period_shared
    if product_names:
        projected['product_names'] = product_names
    projected['rows'] = packed
    if _qev_redundant(response):
        projected.pop('qev', None)
    # ／：共享业务范围解析回显（模型只需范围/覆盖/上界，不需要成员长名单——成员已由
    # rows 逐商品给出；品类成员不由模型枚举）。裁剪走单点 `_project_scope_resolution`，
    # 与筛选面同形。
    scope_resolution = _project_scope_resolution(response.get('scope_resolution'))
    if scope_resolution is not None:
        projected['scope_resolution'] = scope_resolution
    # 品类合计与成员贡献（模型可见投影）——数值/覆盖全部取 Data 原值，只把 canonical
    # 引用换成短 handle；贡献度分母绑定同一份 `category_total:<metric>:<category>`，
    # 子集合计另用 `category_selected_total`，两者不混称。
    category_totals = response.get('category_totals')
    if isinstance(category_totals, dict) and category_totals:
        projected_totals, total_handles = {}, {}
        for metric_key, block in category_totals.items():
            if not isinstance(block, dict):
                continue
            groups = []
            for entry in (block.get('categories') or []):
                if not isinstance(entry, dict):
                    continue
                handle = handle_by_canon.get(str(entry.get('ref') or ''))
                item = {
                    'category': entry.get('category'),
                    'catalog_member_count': entry.get('catalog_member_count'),
                    'member_count': entry.get('member_count'),
                    'coverage_complete': entry.get('coverage_complete'),
                    'complete': entry.get('complete'),
                    'sum': entry.get('sum'),
                    'selected_n': entry.get('selected_n'),
                    'comparable_n': entry.get('comparable_n'),
                    'non_comparable_n': entry.get('non_comparable_n'),
                    'non_comparable_by_state': entry.get('non_comparable_by_state'),
                    'member_scope': entry.get('member_scope'),
                }
                if handle:
                    item['ref'] = handle
                    total_handles[handle] = {'metric': metric_key,
                                             'category': entry.get('category'),
                                             'member_scope': entry.get('member_scope')}
                full = entry.get('full_category_total')
                if isinstance(full, dict):
                    full_handle = handle_by_canon.get(str(full.get('ref') or ''))
                    item['full_category_total'] = {
                        'sum': full.get('sum'),
                        'complete': full.get('complete'),
                        'comparable_n': full.get('comparable_n'),
                        'non_comparable_n': full.get('non_comparable_n'),
                        'non_comparable_by_state': full.get('non_comparable_by_state'),
                        **({'ref': full_handle} if full_handle else {}),
                    }
                    if full_handle:
                        total_handles[full_handle] = {'metric': metric_key,
                                                      'category': entry.get('category'),
                                                      'member_scope': 'full_category'}
                groups.append(item)
            projected_totals[metric_key] = {'group_by': block.get('group_by'),
                                            'categories': groups}
        if projected_totals:
            projected['category_totals'] = projected_totals
            if total_handles:
                projected['category_total_handles'] = total_handles
    # 单期集合合计（value aggregate='sum'）的模型可见投影——显式暴露集合级合计值、
    # 原始成员、覆盖计数、状态分类与对应的 `set_sum:<metric>` 证据 handle，使模型在生成
    # 正文前即可引用实际 sum 和 handle，不把求和留到终答后由展示层复算。
    spt = response.get('single_period_totals')
    if isinstance(spt, dict) and spt:
        # `single_period_totals` 是 {metric: entry} dict（每个可加总指标一条独立合计）
        projected_spt = {}
        for metric_key, entry in spt.items():
            if not isinstance(entry, dict) or entry.get('event') != 'single_period_totals':
                continue
            ref = 'pqev:%s:set_sum:%s' % (qid, metric_key)
            handle = handle_by_canon.get(ref)
            projected_spt[metric_key] = {
                'metric': metric_key,
                'metric_name': entry.get('metric_name'),
                'unit': entry.get('unit'),
                'aggregate': entry.get('aggregate'),
                'sum': entry.get('sum'),
                'complete': entry.get('complete'),
                'sum_scope': entry.get('sum_scope'),
                'selected_n': entry.get('selected_n'),
                'comparable_n': entry.get('comparable_n'),
                'non_comparable_n': entry.get('non_comparable_n'),
                'non_comparable_by_state': entry.get('non_comparable_by_state'),
                'product_ids': entry.get('product_ids'),
                'period_id': (response.get('period') or {}).get('period_id'),
                'start_date': ((response.get('window') or {}).get('start_date')
                               or (response.get('window') or {}).get('period_start_date')),
                'end_date': ((response.get('window') or {}).get('end_date')
                             or (response.get('window') or {}).get('period_end_date')),
                'note': entry.get('note'),
                **({'ref': handle} if handle else {}),
            }
        if projected_spt:
            projected['single_period_totals'] = projected_spt
    projected['projection_note'] = (
        'period_values 模型投影：行级只留 product_id/metric/value/state 与判断所需分母与计算'
        '字段＋证据 handle；重复的指标定义与周期口径已提到 metric_defs/period_shared/'
        'product_names 共享区一次；qev 与 evidence_handles 值集可证等价时不再重复下发；'
        '商品与 unknown 状态均未截断，原始响应与证据回查不受影响')
    return projected


_STATE_OBJECT_KEEP = ('identity', 'comparison_window')
_STATE_OBJECT_LIST_KEYS = ('states', 'state_changes', 'attribute_drift', 'record_conflicts')


def _project_period_object_states(response: dict, query: dict) -> dict:
    """period_object_states 模型可见投影：只压缩多商品成功响应，单商品与异常响应原样返回。

    每商品只保留 product_id、identity、comparison_window、非空的 states / state_changes /
    attribute_drift / record_conflicts、对应证据 handle；每商品重复的 contract/demo 口径说明
    只在共享区（响应顶层）出现一次。状态与变化事实一条不删（不截断商品、不丢 unknown 状态）。
    """
    objs = response.get('objects')
    if query.get('op') != 'states' or response.get('event') != 'period_object_states':
        return response
    requested = query.get('product_ids')
    n_products = len(requested) if isinstance(requested, list) else len(objs if isinstance(objs, list) else [])
    if (n_products < 2 or not isinstance(objs, list) or len(objs) < 2
            or any(not isinstance(obj, dict) for obj in objs)):
        return response
    qid, handle_by_canon = _handle_lookup(response)
    if not handle_by_canon:
        return response

    packed = []
    for obj in objs:
        product_id = str(obj.get('product_id') or '')
        item: dict = {'product_id': product_id}
        for key in _STATE_OBJECT_KEEP:
            if obj.get(key) is not None:
                item[key] = obj[key]
        for key in _STATE_OBJECT_LIST_KEYS:
            value = obj.get(key)
            if value:
                item[key] = value
        handle = handle_by_canon.get('pqev:%s:object_state:%s' % (qid, product_id))
        if handle:
            item['ref'] = handle
        packed.append(item)

    projected = {key: value for key, value in response.items()
                 if key not in ('objects', 'provenance', 'entry_contract_version',
                                'contract_version', 'view_version', 'generator_version', 'seed')}
    projected['objects'] = packed
    if _qev_redundant(response):
        projected.pop('qev', None)
    projected['projection_note'] = (
        'period_object_states 模型投影：每商品只留 identity/comparison_window 与非空的'
        ' states/state_changes/attribute_drift/record_conflicts＋证据 handle；每商品重复的'
        ' contract/demo 说明只在顶层出现一次；qev 与 evidence_handles 值集可证等价时不再'
        '重复下发；商品与状态事实均未截断')
    return projected


_SET_COMPARE_GROUP_KEYS = ('high_group', 'equal_group', 'low_group')


def _set_compare_member_partition(response: dict) -> list | None:
    """set_compare 分组 → 成员 ID 全集；分组未构成完整分区（或缺计数核对）时返回 None。

    只做**可证明的**去重前置判定：median 的 high/equal/low 与 rank 的 ranking 是可用成员
    分区，missing_group 是其余成员；两者并集必须恰好等于响应声明的成员全集，且规模与
    `usable_count`/`missing_count` 一致、成员不重复。任一条不成立＝不满足投影合同，
    调用方原样返回（fail-closed：只在成员可证等价时才省略 `objects`）。
    """
    kind = str(response.get('kind') or '')
    missing = response.get('missing_group')
    if not isinstance(missing, list):
        return None
    missing_ids = []
    for item in missing:
        if not isinstance(item, dict) or not isinstance(item.get('product_id'), str):
            return None
        missing_ids.append(item['product_id'])
    usable_ids: list = []
    if kind == 'rank':
        ranking = response.get('ranking')
        if not isinstance(ranking, list):
            return None
        for row in ranking:
            if not isinstance(row, dict) or not isinstance(row.get('product_id'), str):
                return None
            usable_ids.append(row['product_id'])
    elif kind == 'median':
        for key in _SET_COMPARE_GROUP_KEYS:
            group = response.get(key)
            if not isinstance(group, list) or any(not isinstance(g, str) for g in group):
                return None
            usable_ids.extend(group)
    else:
        return None
    if (response.get('usable_count') != len(usable_ids)
            or response.get('missing_count') != len(missing_ids)):
        return None
    members = usable_ids + missing_ids
    if len(members) != len(set(members)):
        return None
    return sorted(members)


def _project_period_set_compare(response: dict, query: dict) -> dict:
    """period_set_compare 模型可见投影：多成员成功响应去掉两处**可证等价**的重复字段。

    去掉的两处（其余事实一条不删）：

    - `qev`：逐项等于 `evidence_handles` 的 canonical 值集（handle→canonical 映射仍全量
      下发，模型照 [E#] 引用）；原件以 `query_id` 登记在根任务 `query_responses`。
    - `objects`：成员全集＝分组分区（median=high/equal/low ＋ missing_group；rank=ranking
      ＋ missing_group），规模仍由 `usable_count`/`missing_count`/`benchmark_disclosure`
      给出；原始成员表同在上述登记响应（`compare:` 引用回查即整份原件）。

    中位数（或名次）、可用/缺失计数、high/equal/low 分组或 ranking、缺失成员与原因码、
    指标口径、周期、`citation_note` 与 handles 全部保留。形状不满足、成员并集与 `objects`
    不等价、或 `qev` 与 handles 值集不一致时一律原样返回（不按字节指标删减事实）。

     说明：请求合同（op=set_compare 只有 product_ids/period_id|time/metric/kind）无法
    表达「摘要 / 分组 / 明细」，本投影**不新增请求字段**、不按 kind 省略任何分组——只做
    上述等价去重；「只取中位数、不要分组」的摘要面留给未来有实测用途时再接线。
    """
    if (query.get('op') != 'set_compare'
            or response.get('event') not in ('period_set_compare', 'period_window_set_compare')):
        return response
    requested = query.get('product_ids')
    members = response.get('objects')
    n_members = len(requested) if isinstance(requested, list) else (
        len(members) if isinstance(members, list) else 0)
    if (n_members < 2 or not isinstance(members, list) or len(members) < 2
            or any(not isinstance(m, str) for m in members)):
        return response
    if _set_compare_member_partition(response) != sorted(members):
        return response
    handles = response.get('evidence_handles')
    qev = response.get('qev')
    if (not isinstance(handles, dict) or not isinstance(qev, list)
            or any(not isinstance(handle, str) or not isinstance(canonical, str)
                   for handle, canonical in handles.items())):
        return response
    if sorted(handles.values()) != sorted(str(ref) for ref in qev):
        return response

    projected = {key: value for key, value in response.items()
                 if key not in ('qev', 'objects', 'provenance', 'entry_contract_version',
                                'contract_version', 'view_version', 'generator_version', 'seed')}
    projected['member_partition_note'] = (
        'set_compare 模型投影：成员全集＝分组分区（median=high/equal/low＋missing_group；'
        'rank=ranking＋missing_group），分组已完整覆盖本次全部 %d 个成员，故不再重复下发'
        '等价的 objects 与 qev 两份副本；中位数、名次、可用/缺失计数、缺失成员与原因码、'
        '指标口径与周期均未删减。完整原件（含 objects/qev）按 citation_note 的 [E#] 引用'
        '回查登记响应，成员规模另见 usable_count/missing_count/benchmark_disclosure'
        % len(members))
    return projected


def _project_cohort_filter(response: dict, query: dict) -> dict:
    """向模型投影筛选条件、范围分母、命中与未知成员及集合引用，不截断名单。

    保留 scope_resolution，使模型能解释范围如何解析。版本和溯源元数据留在登记原件，
    不影响证据回查；非成功响应或形状不符时原样返回。"""
    if (query.get('op') != 'cohort_filter'
            or response.get('event') != 'period_cohort_filter'
            or not isinstance(response.get('matches'), list)
            or not isinstance(response.get('unknown'), list)):
        return response
    keep = ('event', 'time_mode', 'scope', 'period_id', 'base_period_id',
            'target_period_id', 'conditions', 'ordered_by', 'scope_count', 'match_count',
            'nonmatch_count', 'predicate_unknown_count', 'confirmed_share_percent',
            'share_denominator', 'members_complete', 'matches', 'unknown', 'query_id',
            'resolved_window', 'qev', 'evidence_handles', 'citation_note', 'root_budget')
    projected = {key: response[key] for key in keep if key in response}
    # `scope_resolution` 与 value 面共用同一裁剪（先经 keep 白名单漏掉原样副本，
    # 再用单点 helper 收窄）——两路径同形，成员清单不下发。
    scope_resolution = _project_scope_resolution(response.get('scope_resolution'))
    if scope_resolution is not None:
        projected['scope_resolution'] = scope_resolution
    projected['projection_note'] = ('cohort_filter 模型投影：成员全量未截断（matches/unknown '
                                    '长度分别等于 match_count/predicate_unknown_count）；原始'
                                    '服务端响应与证据回查以 evidence_handles 指向的登记为准')
    return projected


_CATALOG_OBJECT_KEEP = ('product_id', 'product_name', 'category', 'city', 'price')


def _project_period_catalog(response: dict, query: dict) -> dict:
    """period_catalog 模型可见投影：objects[] 只留 5 个业务键。"""
    if (query.get('op') != 'catalog' or response.get('event') != 'period_catalog'
            or not isinstance(response.get('objects'), list)
            or any(not isinstance(obj, dict) for obj in response['objects'])):
        return response
    packed = [{key: obj[key] for key in _CATALOG_OBJECT_KEEP if key in obj}
              for obj in response['objects']]
    # 模型投影不携带生成器元数据；页面目录信息从登记原件读取。
    projected = {key: value for key, value in response.items()
                 if key not in ('provenance', 'entry_contract_version', 'contract_version',
                                'view_version', 'generator_version', 'seed')}
    projected['objects'] = packed
    projected['projection_note'] = (
        'catalog 模型投影：objects[] 只保留 product_id/product_name/category/city/price 五个'
        '业务键（逐值不变；`price` 与入口目录下发面同取 `PVG.PRODUCTS[].price` 单点）；'
        'metrics/periods/notes 口径与边界声明原样保留')
    return projected


def _project_daily_series(response: dict, query: dict) -> dict:
    """period_daily_series 模型可见投影：只压缩多对象/多指标的成功逐日序列。

     新增（一次调用取回连续多日读数，避免「七天查七次」）。每行的 `range` 块（逐行重复的
    指标定义/单位/口径/窗口元数据）与共享窗口字段提到共享区；行级只留 product_id/metric 与
    逐日 `days`（date/value/state/unknown_reason 原样，不截断、不代 0）＋对应证据 handle。
    数据层本就按 op/事件/请求一致构造，形状不符（单对象、非 daily_series、缺 handle）一律
    原样返回（fail-closed）。原始响应与证据回查不受影响。
    """
    if query.get('op') != 'daily_series' or response.get('event') != 'period_daily_series':
        return response
    rows = response.get('results')
    requested = query.get('product_ids')
    metrics = query.get('metrics')
    n_products = len(requested) if isinstance(requested, list) else len(response.get('objects') or [])
    n_metrics = len(metrics) if isinstance(metrics, list) else 0
    if (n_products < 2 or n_metrics * n_products < 2 or not isinstance(rows, list)
            or len(rows) < 2 or any(not isinstance(row, dict) for row in rows)):
        return response
    qid, handle_by_canon = _handle_lookup(response)
    if not handle_by_canon:
        return response

    metric_range_defs: dict[str, dict] = {}
    # 每行 range 内跨行同值的键（指标定义 + 窗口元数据）上提到共享区；逐行不同的键
    # （product_id/value/state/denominator/n_rows 等）留在行内，不丢任何 per-行事实。
    holders: dict[str, dict[str, list]] = {}
    for row in rows:
        metric = str(row.get('metric') or '')
        rng = row.get('range') if isinstance(row.get('range'), dict) else {}
        slot = holders.setdefault(metric, {})
        for key, value in rng.items():
            slot.setdefault(key, []).append(value)
    shared_range_keys: dict[str, set] = {}
    for metric, slot in holders.items():
        n = sum(1 for row in rows if str(row.get('metric') or '') == metric)
        metric_range_defs[metric] = {
            key: values[0] for key, values in slot.items()
            if len(values) == n and _same_value(values)}
        shared_range_keys[metric] = set(metric_range_defs[metric])

    packed = []
    for row in rows:
        metric = str(row.get('metric') or '')
        rng = row.get('range') if isinstance(row.get('range'), dict) else {}
        shared = shared_range_keys.get(metric, set())
        item = {key: value for key, value in row.items() if key != 'range'}
        item['range'] = {key: value for key, value in rng.items() if key not in shared}
        handle = handle_by_canon.get('pqev:%s:day_series:%s:%s'
                                     % (qid, row.get('product_id'), row.get('metric')))
        if handle:
            item['ref'] = handle
        packed.append(item)

    projected = {key: value for key, value in response.items()
                 if key not in ('results', 'provenance', 'entry_contract_version',
                                'contract_version', 'view_version', 'generator_version', 'seed')}
    projected['metric_ranges'] = metric_range_defs
    projected['rows'] = packed
    if _qev_redundant(response):
        projected.pop('qev', None)
    projected['projection_note'] = (
        'period_daily_series 模型投影：逐日序列行只留 product_id/metric/days 与证据 handle；'
        '重复的指标定义与窗口口径已提到 metric_ranges 共享区一次（含真实起止日期与粒度）；'
        'days 内 date/value/state/unknown_reason 原样保留，不逐日截断、不代 0；'
        '窗口起止见顶层 window，原始响应与证据回查不受影响')
    return projected


# 模型可见投影按 op 分流单点：cohort_filter/diff/value/states/set_compare/catalog/daily_series
# 各走各自的压缩器；其余 op 原样。
_MODEL_PROJECTIONS = {
    'cohort_filter': _project_cohort_filter,
    'diff': _project_period_diff_set,
    'value': _project_period_values,
    'states': _project_period_object_states,
    'set_compare': _project_period_set_compare,
    'catalog': _project_period_catalog,
    'daily_series': _project_daily_series,
}


def _frozen_params(tool_name: str) -> dict:
    return copy.deepcopy(_FROZEN[tool_name]['parameters'])

def _classify_failure(error: Exception) -> tuple[str, str]:
    """SDK 层异常 → (稳定原因码, 模型可见说明)。

    参数拒绝在 SDK 内统一折成 `ModelBehaviorError("Invalid JSON input for tool …")`（既覆盖
    非法 JSON，也覆盖 schema 校验失败），故只分「是不是参数层被拒」一类，不为 Pydantic 错误
    种类造分类体系；说明不含 traceback。
    """
    if isinstance(error, ModelBehaviorError):
        if str(error).startswith('Invalid JSON input for tool'):
            return ('tool_invalid_args',
                    '工具参数不符合该工具的参数合同（未进入业务工具，本次不消耗预算）：'
                    '请检查 JSON 结构与必填字段后重试')
        return ('tool_behavior_error', '模型发出的工具调用不可执行（未返回业务结果）')
    return ('tool_execution_error', '工具执行异常：%s（未返回业务结果）' % type(error).__name__)


def tool_failure_error(ctx: RunContextWrapper[Any], error: Exception) -> str:
    """最小自定义 `failure_error_function`：SDK 层错误 → 结构化错误文本。"""
    code, detail = _classify_failure(error)
    context = getattr(ctx, 'context', None)
    if isinstance(context, RunContext):
        context.record_step(step_id=context.new_step_id(), kind='tool',
                            tool=str(getattr(ctx, 'tool_name', '') or ''),
                            args=str(getattr(ctx, 'tool_arguments', '') or ''),
                            status='failed', elapsed_ms=0, result_code=code)
    return json.dumps({'ok': False, 'error_code': code, 'detail': detail}, ensure_ascii=False)


def tool_error_text(args: ToolErrorFormatterArgs) -> str | None:
    """`RunConfig.tool_error_formatter`：模型调到工具面外的名字时，返回结构化拒绝并让循环继续。"""
    if args.kind != 'tool_not_found':
        return None
    context = args.run_context.context
    if isinstance(context, RunContext):
        context.record_step(step_id=context.new_step_id(), kind='guard', tool=args.tool_name,
                            args={}, status='rejected', elapsed_ms=0, result_code='unknown_tool')
    return json.dumps({'ok': False, 'error_code': 'unknown_tool',
                       'detail': args.default_message + '（主链工具面只含 %s 与 knowledge_search；'
                                                        '本工具未执行）'
                                 % ' / '.join(tuple(mcp_contract.TOOL_OPS) + ('oagent_query',)),
                       }, ensure_ascii=False)


async def _run_data_tool(ctx: RunContextWrapper[RunContext], tool_name: str, args: dict,
                         call: Callable[[], Any], *, model_query: dict | None = None,
                         step_args: dict | None = None,
                         error_code: str = 'tool_http_error') -> str:
    """执行数据工具的预算闸、暂记扣数、查询、步骤登记与模型可见投影。

用于 oagent_query；MCP 查询通过输入/输出守卫接入共用处理。
服务端回显校准额度，未获得权威回显的失败按未对账记录处理。"""
    rejection = _query_budget_gate(ctx, tool_name, args)
    if rejection is not None:
        return tool_response_text(rejection)
    if model_query is None and tool_name == 'oagent_query':
        model_query = (args.get('query') if isinstance(args.get('query'), dict) else None)
    t0 = time.monotonic()
    try:
        resp = call()
        if inspect.isawaitable(resp):
            resp = await resp
    except Exception as exc:   # noqa: BLE001
        resp = {'ok': False, 'error_code': error_code,
                'detail': '%s: %s' % (type(exc).__name__, exc)}
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _query_result_notes(ctx, tool_name, resp, args=args, step_args=step_args,
                        model_query=model_query, elapsed_ms=elapsed_ms)
    return tool_response_text(_project_model_response(resp, model_query))


def _query_budget_gate(ctx: RunContextWrapper[RunContext], tool_name: str, args: Any) -> dict | None:
    """查询**调用前**共用段：预算闸 → 扣数（`oagent_query` 与原生 MCP 接入点共用）。

    被闸拒绝时登记 `guard/rejected` 步骤并返回服务端形状的拒绝响应（`budget_reached`）——
    调用方据此**不发起**真实查询；允许时先本地暂记一次额度（收到 `root_budget` 后由
    `note_server_budget` 校准），返回 None。暂记是**乐观**的：拿不到权威回显的故障由共用
    后处理回滚（`note_unreconciled_failure`），不认定为已消耗。
    """
    context = ctx.context
    gate = context.gate(tool_name)
    if not gate['allowed']:
        context.record_step(step_id=context.new_step_id(), kind='guard', tool=tool_name,
                            args=args, status='rejected', elapsed_ms=0, result_code=gate['code'])
        return context.budget_reject(tool_name, gate['detail'])
    context.charge(tool_name)
    return None


def _query_result_notes(ctx: RunContextWrapper[RunContext], tool_name: str, resp: Any, *,
                        args: Any, step_args: Any, model_query: dict | None,
                        elapsed_ms: int) -> None:
    """查询**调用后**共用段：服务端额度对账 → 步骤登记 → 运行期快照（证据/展示登记）。

    `resp` 是数据层原始响应（业务拒绝也是它）；`model_query`＝该次查询对应的数据层请求，
    为 None 时只做对账与步骤登记、不登记快照与投影。所有快照都走已存在的 `RunContext`
    方法，不在此复算数值、不新建登记面。

    对账口径：只有拿到服务端 `root_budget` 才算校准；拿不到（传输/适配层故障）
    时调用前的乐观暂记不认定为已消耗有效查询额度，回滚并登记不确定次数。两条查询通道
    （`oagent_query` 直调 / 原生 MCP 接入点）共用这一判定。
    """
    context = ctx.context
    if not context.note_server_budget(resp):
        context.note_unreconciled_failure(tool_name)
    status = tool_call_status(resp)
    result_code = ((str(resp.get('error_code') or resp.get('event') or 'unknown'))
                   if status != 'success'
                   else (str(resp.get('event')) if resp.get('event') else 'ok'))
    context.record_step(step_id=context.new_step_id(), kind='tool', tool=tool_name,
                        args=step_args if step_args is not None else args,
                        status=status, elapsed_ms=elapsed_ms,
                        result_code=result_code)
    if model_query is not None:
        # 成功查询的品类合计来源登记进 RunContext 运行期快照（供展示兜底；不改原始
        # 响应、不动 Evidence 登记；不合规输入静默跳过）——diff 登记 Data 已算好的
        # category_totals，value 登记紧凑周期值（兜底装配时配对合成，覆盖模型走 value×2
        # 而不用 diff 的路径）。
        context.note_category_totals(model_query, resp)
        context.note_period_values(model_query, resp)
        context.note_period_object_states(model_query, resp)
        # 登记本轮**实际请求范围**（调查对象/比较基准的可信来源；只记成功查询、只存紧凑
        # 字段，不保存响应体、不动预算与证据登记）——供 run_finished.query_scope 结构化下发。
        context.note_query_scope(model_query, resp)


def _project_model_response(resp: Any, model_query: dict | None) -> Any:
    """模型可见投影单点：按 op 分流投影器（`resp` 原件不动）。

    `model_query` 为 None 时原样返回（无 op 可分）。投影器对形状不符的输入一律原样返回。
    模型可见面的统一清理（已退出能力的额度槽、内部实现标签）在 `tool_response_text` 出口
    统一做——成功、单商品、拒绝、异常响应同口径，不再按 op 分流。
    """
    if model_query is None:
        return resp
    projector = _MODEL_PROJECTIONS.get(str(model_query.get('op') or ''))
    return projector(resp, model_query) if projector else resp


@function_tool(name_override='oagent_query', description_override=_FROZEN['oagent_query']['description'],
               use_docstring_info=False, strict_mode=False,
               failure_error_function=tool_failure_error)
async def oagent_query(ctx: RunContextWrapper[RunContext], query: dict) -> str:
    """只读查询 states 与 daily_series，运行 ID 由客户端注入。

其它业务操作返回结构化拒绝并指向对应 MCP 工具，不消耗查询额度。"""
    op = str(query.get('op') or '') if isinstance(query, dict) else ''
    if op in _MIGRATED_OP_TOOL:
        ctx.context.record_step(step_id=ctx.context.new_step_id(), kind='guard',
                                tool='oagent_query', args={'query': query}, status='rejected',
                                elapsed_ms=0, result_code='op_migrated')
        return tool_response_text({
            'ok': False, 'error_code': 'op_migrated',
            'detail': 'op=%s 已迁移到独立查询工具 %s（本工具只承载 states / daily_series；'
                      '本次未执行查询，不消耗额度）'
                      % (op, _MIGRATED_OP_TOOL[op])})
    if op in _INTERNAL_ONLY_OPS:
        # `catalog` 退出模型可选 op 面——可信目录只作内部范围解析/运行期上下文用途。
        # 模型要目录内容改用任务输入里已下发的商品目录，或各查询工具的 `scope`。
        ctx.context.record_step(step_id=ctx.context.new_step_id(), kind='guard',
                                tool='oagent_query', args={'query': query}, status='rejected',
                                elapsed_ms=0, result_code='op_internal_only')
        return tool_response_text({
            'ok': False, 'error_code': 'op_internal_only',
            'detail': 'op=%s 不再是模型可选操作（可信目录只用于内部范围解析；'
                      '商品目录随任务输入下发，取数范围用各查询工具的 scope 表达：'
                      '全池 / 一个或多个目录品类 / 显式商品集合）。本次未执行查询，不消耗额度。'
                      % op})
    return await _run_data_tool(
        ctx, 'oagent_query', {'query': query},
        lambda: asyncio.to_thread(ctx.context.client.query, ctx.context.run_id, query))


# ---------------- ：原生 MCP 四个查询工具的查询前后处理接入点 ----------------
# 工具定义与执行由 SDK 原生 `Agent.mcp_servers` 承担（`tools/list` / `tools/call`），本文件
# 只保留调用前/调用后的处理，挂在 MCP 会话的 `tool_input_guardrails` /
# `tool_output_guardrails` 上（运行层经 `mcp_query_tools.client.MCPQueryClient.configure_guardrails`
# 注入）。细粒度结构（枚举、时间三选一、op 键闭集）由原生发现的 Schema（= 合同）与
#  Server + 数据层共同保证；接入点只做四件事：会话检查 → 合同类型保护 → 预算闸 →
# 固定 op 请求（`to_request`，参数里的 run_id 被丢弃）。


def _mcp_request(tool_name: str, args: dict) -> tuple[dict | None, list[str]]:
    """MCP 工具参数 → `(数据层 request, 类型错误列表)`：类型不符时不构造请求（返回 None）。

    数组参数先过合同同款 `check_arg_types`（误传字符串会被 `list(...)` 拆成字符列表，
    SDK 不代校验）；细粒度结构（枚举、时间三选一、op 键闭集）仍由  Server
    与数据层按其合同拒绝，本函数不预判业务语义。与 `oagent_query` 的共用段同源。
    """
    type_errors = mcp_contract.check_arg_types(tool_name, args)
    if type_errors:
        return None, type_errors
    try:
        return mcp_contract.to_request(tool_name, args), []
    except (KeyError, TypeError, ValueError) as exc:
        return None, ['请求参数无法构造：%s' % type(exc).__name__]


def _mcp_invalid_args_response(type_errors: list[str]) -> dict:
    return {
        'ok': False, 'error_code': 'mcp_invalid_args',
        'detail': '参数不合法：%s（未向数据服务发出查询，不消耗查询额度）'
                  % '；'.join(type_errors or ['请求构造失败'])}


_EMPTY_MCP_SESSION_RESPONSE = {
    'ok': False, 'error_code': 'mcp_unavailable',
    'detail': '本 run 的 MCP 查询会话未建立，本次查询未执行（不静默改走旧入口）'}


# ---------------- ：原生 MCP 接入点（SDK `MCPServerStdio.tool_*_guardrails`） ----------------
# `mcp_query_input_guardrail` / `mcp_query_output_guardrail` 把查询前后处理挂到 SDK 原生 MCP
# 工具的调用前/调用后扩展点上，调用的是与 `oagent_query` 同一批共用函数
# （`_query_budget_gate` / `_query_result_notes` / `_project_model_response`），不并存第二套
# 预算/证据/投影。运行层把它们注入 MCP 会话（`configure_guardrails`），模型面因此只有一份入口。


def _guardrail_context(data: Any) -> tuple[str, dict | None]:
    """guardrail data → `(tool_name, args)`；参数不是 JSON 对象时 args=None（调用前拒绝）。"""
    context = getattr(data, 'context', None)
    tool_name = str(getattr(context, 'tool_name', '') or '')
    raw = getattr(context, 'tool_arguments', '') or ''
    try:
        args = json.loads(raw) if raw else {}
    except ValueError:
        return tool_name, None
    return tool_name, (args if isinstance(args, dict) else None)


_MCP_TEXT_BLOCK_TYPES = ('text', 'input_text')


def _mcp_output_text(output: Any) -> str:
    """SDK MCP 工具输出 → 可读原文（故障说明用）；取不到时返回空串。

    SDK 在不同路径下给出不同形状：`use_structured_content=True` 且非故障＝纯 JSON 字符串；
    文本块＝`{'type':'text','text':…}`；工具抛错经 `failure_error_function` 后可能被塑成
    输入项列表（`[{'type':'input_text','text':…}]`）。这里只做「取回原文」，不做语义解释。
    """
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        text = output.get('text') if output.get('type') in _MCP_TEXT_BLOCK_TYPES else None
        return text if isinstance(text, str) else ''
    if isinstance(output, list):
        parts = [block.get('text') for block in output
                 if isinstance(block, dict) and isinstance(block.get('text'), str)]
        return '；'.join(parts)
    return ''


def _mcp_output_payload(output: Any) -> dict | None:
    """SDK MCP 工具输出 → 数据层响应 dict；非 JSON 对象（适配层/传输故障文本）返回 None。

    兼容 `use_structured_content` 两种形态：True＝纯 JSON 字符串；False＝`{'type':'text',...}`
    文本块（或长度 1 的文本块列表）。业务拒绝也是 JSON（`period_rejected` 等），会被解析出来
    走正常对账/步骤；只有 MCP 适配层/传输故障才是非 JSON 文本。
    """
    text = _mcp_output_text(output)
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def mcp_query_input_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """原生 MCP 查询工具的**调用前**接入点：会话缺失 / 参数类型 / 预算闸三类拒绝在真实
    `tools/call` 之前以 `reject_content` 返回（不发请求、不扣额度）；放行时暂记额度并记下
    调用起点供输出钩子算 elapsed。

    参数从 `data.context.tool_arguments`（SDK 原样透传的 JSON 字符串）解析；`data.context`
    是 `ToolContext`（`RunContextWrapper` 子类），`data.context.context` 即本 run 的 `RunContext`。
    参数不是 JSON 对象（畸形 JSON / 数组 / 标量）时按 `mcp_invalid_args` 拒绝——预算闸、
    步骤登记与额度暂记不得因参数不可解析而整链跳过（fail-closed，不押注 Server 必填兜底）。
    """
    tool_name, args = _guardrail_context(data)
    if tool_name not in mcp_contract.TOOL_OPS:
        return ToolGuardrailFunctionOutput.allow()
    if args is None:
        context = data.context.context
        context.record_step(step_id=context.new_step_id(), kind='tool', tool=tool_name,
                            args=None, status='rejected', elapsed_ms=0,
                            result_code='mcp_invalid_args')
        return ToolGuardrailFunctionOutput.reject_content(
            tool_response_text(_mcp_invalid_args_response(
                ['工具参数不是 JSON 对象（畸形 JSON / 数组 / 标量）'])))
    context = data.context.context
    if getattr(context, 'mcp', None) is None:
        context.record_step(step_id=context.new_step_id(), kind='tool', tool=tool_name,
                            args=args, status='failed', elapsed_ms=0,
                            result_code='mcp_unavailable')
        return ToolGuardrailFunctionOutput.reject_content(
            tool_response_text(dict(_EMPTY_MCP_SESSION_RESPONSE)))
    request, type_errors = _mcp_request(tool_name, args)
    if request is None:
        context.record_step(step_id=context.new_step_id(), kind='tool', tool=tool_name,
                            args=args, status='rejected', elapsed_ms=0,
                            result_code='mcp_invalid_args')
        return ToolGuardrailFunctionOutput.reject_content(
            tool_response_text(_mcp_invalid_args_response(type_errors)))
    rejection = _query_budget_gate(data.context, tool_name, args)
    if rejection is not None:
        return ToolGuardrailFunctionOutput.reject_content(tool_response_text(rejection))
    context.mcp_tool_started_at[str(getattr(data.context, 'tool_call_id', '') or '')] = \
        time.monotonic()
    return ToolGuardrailFunctionOutput.allow()


def mcp_query_output_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    """原生 MCP 查询工具的**调用后**接入点：对账 + 步骤 + 快照 + 模型可见投影替换。

    与 `oagent_query` 同一批实现：`_query_result_notes` 记服务端额度/步骤/快照，
    `_project_model_response` 出压缩结果。业务拒绝（JSON + `event`）走同一路径；MCP 适配层/
    传输故障（非 JSON 文本）登记 `failed`/`mcp_tool_error`、回滚调用前的乐观暂记（无权威回显
    ⇒ 不认定已消耗有效查询额度），并把原因码与可读原文按共用段的同一形状回给模型，
    不伪装成业务拒绝。

    SDK 在工具抛错时仍会走到这里（`failure_error_function` 产出的文本先于输出钩子被
    `_execute_tool_output_guardrails` 拿到，见 `agents/run_internal/tool_execution.py`），故
    「非 JSON 文本 ⇒ `mcp_tool_error`」是本链路唯一的故障归类点，不需要在 Server/Agent 侧再设
    第二套错误文案。被调用前钩子 `reject_content` 的调用**不会**到达这里（已跳过）。
    """
    tool_name, args = _guardrail_context(data)
    if tool_name not in mcp_contract.TOOL_OPS:
        return ToolGuardrailFunctionOutput.allow()
    if args is None:
        # 正常配对下不可达（调用前钩子已按 mcp_invalid_args 拒绝、不会再执行到输出钩子）；
        # 只兜未配对装配/异常路径：调用已发生但参数不可解析——只做对账与步骤登记，
        # 不静默跳过；也不回滚额度（无法证明调用前是否暂记过，回滚可能误伤其它调用的暂记）。
        context = data.context.context
        resp = _mcp_output_payload(getattr(data, 'output', None))
        status = tool_call_status(resp) if isinstance(resp, dict) else 'failed'
        if isinstance(resp, dict) and resp.get('root_budget'):
            context.note_server_budget(resp)    # 有权威回显则照常校准本地镜像
        context.record_step(step_id=context.new_step_id(), kind='tool', tool=tool_name,
                            args=None, status=status, elapsed_ms=0,
                            result_code=(str(resp.get('error_code') or resp.get('event')
                                               or 'mcp_invalid_args')
                                         if isinstance(resp, dict) else 'mcp_invalid_args'))
        return ToolGuardrailFunctionOutput.allow()
    ctx = data.context
    context = ctx.context
    started = context.mcp_tool_started_at.pop(str(getattr(ctx, 'tool_call_id', '') or ''), None)
    elapsed_ms = int((time.monotonic() - started) * 1000) if started else 0
    request, _ = _mcp_request(tool_name, args)
    resp = _mcp_output_payload(getattr(data, 'output', None))
    if resp is None:
        # MCP 适配层/传输故障：`is_error` 文本或 SDK `failure_error_function` 文本（非 JSON）。
        # 无服务端权威回显 ⇒ 回滚调用前的乐观暂记，不认定已消耗有效查询额度（与共用段同口径）；
        # 并按 FunctionTool 链同一形状回结构化失败（稳定原因码 `mcp_tool_error` + 可读原文），
        # 不把 SDK 的原始输出形状泄漏给模型、也不伪装成业务拒绝。
        context.note_unreconciled_failure(tool_name)
        context.record_step(step_id=context.new_step_id(), kind='tool', tool=tool_name,
                            args=request if request is not None else args, status='failed',
                            elapsed_ms=elapsed_ms, result_code='mcp_tool_error')
        return ToolGuardrailFunctionOutput.reject_content(tool_response_text({
            'ok': False, 'error_code': 'mcp_tool_error',
            'detail': (_mcp_output_text(getattr(data, 'output', None))
                       or 'MCP 查询工具故障（无返回内容）')}))
    _query_result_notes(ctx, tool_name, resp, args=args, step_args=request,
                        model_query=request, elapsed_ms=elapsed_ms)
    projected = _project_model_response(resp, request)
    return ToolGuardrailFunctionOutput.reject_content(tool_response_text(projected))


# 注入 MCP 会话的接入点单点（`MCPServerStdio(tool_input_guardrails=[...], tool_output_guardrails=[...])`）。
# 运行层在 `configure_guardrails` 里传的是这两个对象：与 `oagent_query` 共用同一批前后处理函数，
# 因此四个查询工具没有第二个模型入口，也不存在第二套预算/证据/投影。
MCP_QUERY_INPUT_GUARDRAIL = ToolInputGuardrail(guardrail_function=mcp_query_input_guardrail,
                                               name='oagent-query-input')
MCP_QUERY_OUTPUT_GUARDRAIL = ToolOutputGuardrail(guardrail_function=mcp_query_output_guardrail,
                                                 name='oagent-query-output')


@function_tool(name_override='knowledge_search',
               description_override=_FROZEN['knowledge_search']['description'],
               use_docstring_info=False, strict_mode=False,
               failure_error_function=tool_failure_error)
async def knowledge_search(ctx: RunContextWrapper[RunContext], query: str,
                          doc_type: str | None = None) -> str:
    """执行本地知识检索，不占业务查询预算，不调用数据服务。

交付粒度由 KNOWLEDGE_DELIVERY_MODE 选择，重复查询受收敛闸限制。"""
    args: dict = {'query': query}
    if doc_type is not None:
        args['doc_type'] = doc_type
    context = ctx.context

    dup_key = (str(query).strip(), doc_type)
    if context.knowledge_closed:
        context.record_step(step_id=context.new_step_id(), kind='guard', tool='knowledge_search',
                            args=args, status='rejected', elapsed_ms=0,
                            result_code='knowledge_search_closed')
        return _knowledge_loop_reject(
            'knowledge_search_closed',
            '本 run 知识检索已关闭（连续 %d 次检索均未返回新内容）。请停止检索，'
            '基于已有信息直接输出最终回答。' % _KNOWLEDGE_CLOSE_STREAK)
    if dup_key in context.knowledge_queries:
        context.record_step(step_id=context.new_step_id(), kind='guard', tool='knowledge_search',
                            args=args, status='rejected', elapsed_ms=0,
                            result_code='knowledge_duplicate_query')
        return _knowledge_loop_reject(
            'knowledge_duplicate_query',
            '本 run 已执行过完全相同的查询（检索为确定性本地索引，重复执行只会返回完全相同的'
            '结果，不会产生新信息）。请换一个不同的检索角度，或直接基于已有信息输出最终回答。')

    t0 = time.monotonic()
    resp = await asyncio.to_thread(knowledge_search_response, context.retriever, args,
                                   KNOWLEDGE_DELIVERY_MODE)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if resp.get('ok'):
        context.knowledge_queries.add(dup_key)
        items = {_knowledge_item_key(r) for r in (resp.get('results') or [])}
        new_items = items - context.knowledge_seen_content
        context.knowledge_seen_content |= items
        # 知识来源登记（展示面）：只记 doc_id/title/doc_type，按 doc_id 去重、保留首次返回顺序；
        # 供 run_finished.knowledge_sources 投影，不进 Evidence Guard、不影响检索与预算。
        for row in (resp.get('results') or []):
            if not isinstance(row, dict):
                continue
            doc_id = str(row.get('doc_id') or '').strip()
            if not doc_id or doc_id in context.knowledge_sources:
                continue
            context.knowledge_sources[doc_id] = {
                'doc_id': doc_id,
                'title': str(row.get('title') or '').strip(),
                'doc_type': str(row.get('doc_type') or '').strip(),
            }
        context.knowledge_no_new_streak = (0 if new_items
                                           else context.knowledge_no_new_streak + 1)
        closed_now = False
        if context.knowledge_no_new_streak >= _KNOWLEDGE_CLOSE_STREAK:
            context.knowledge_closed = True
            closed_now = True
        resp['knowledge_note'] = _knowledge_note(len(new_items), len(items or []),
                                                 closed_now)
    result_code = ('ok' if resp.get('ok') else str(resp.get('error_code') or 'unknown'))
    context.record_step(step_id=context.new_step_id(), kind='tool', tool='knowledge_search',
                        args=args, status=tool_call_status(resp), elapsed_ms=elapsed_ms,
                        result_code=result_code)
    return tool_response_text(resp)


def build_tools() -> list[FunctionTool]:
    """装配 oagent_query 与 knowledge_search 两个 FunctionTool，参数 schema 取自 CURRENT_OPENAI_TOOLS。

    四个 MCP 查询工具由 SDK 会话发现，本函数不重复包装。"""
    tools = [oagent_query, knowledge_search]
    for tool in tools:
        tool.params_json_schema = _frozen_params(tool.name)
    return tools
