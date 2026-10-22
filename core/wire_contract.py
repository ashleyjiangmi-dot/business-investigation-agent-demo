# -*- coding: utf-8 -*-
"""SSE 下发合同：事件白名单、查询摘要、引用详情、业务报告与展示块投影。

只返回前端需要的字段；校验输出形状并过滤内部响应与执行细节。"""
from __future__ import annotations

import json
import re
from collections.abc import Generator

from .tool_client import MCP_QUERY_TOOLS

QUERY_PARAM_KEYS = ('op', 'product_ids', 'period_id', 'metrics', 'metric', 'kind',
                    'base_period_id', 'target_period_id', 'scope', 'conditions')
EVIDENCE_PARAM_KEYS = ('product_id', 'period_id', 'metrics')
KNOWLEDGE_PARAM_KEYS = ('query', 'doc_type')


def _filter_params(tool: str, digest: str) -> str:
    """按白名单过滤工具步骤参数并序列化；解析失败时返回空串。

    MCP 查询参数是含 op 的扁平请求，与 oagent_query 使用相同的展示白名单。"""
    if tool == 'oagent_query':
        keys = QUERY_PARAM_KEYS
    elif tool in MCP_QUERY_TOOLS:
        keys = QUERY_PARAM_KEYS
    elif tool == 'oagent_evidence':
        keys = EVIDENCE_PARAM_KEYS
    elif tool == 'knowledge_search':
        keys = KNOWLEDGE_PARAM_KEYS
    else:
        return ''          # guard/delegation 参数不下发（专项请求含模型生成正文）
    try:
        args = json.loads(digest) if isinstance(digest, str) and digest.strip() else None
    except json.JSONDecodeError:
        return ''
    args = (args or {}).get('query') if tool == 'oagent_query' and isinstance(args, dict) else args
    if not isinstance(args, dict):
        return ''
    return json.dumps({k: args[k] for k in keys if k in args}, ensure_ascii=False)


def _wire_step_events(events: list | None) -> Generator[dict, None, None]:
    """图内可观测事件 → SSE 白名单事件。非步骤事件（root_init_ok/refs_extracted/…
    delegation_finished 等内部排查件）不过线。"""
    for ev in events or []:
        t = ev.get('type')
        if t == 'step_started':
            kind = str(ev.get('kind') or 'tool')
            yield {'event': 'step_started',
                   'data': {'step_id': str(ev.get('step_id') or ''), 'kind': kind,
                            'label': str(ev.get('label') or ''),
                            'params_digest': _filter_params(str(ev.get('label') or ''),
                                                            str(ev.get('params_digest') or ''))}}
        elif t == 'step_finished':
            yield {'event': 'step_finished',
                   'data': {'step_id': str(ev.get('step_id') or ''),
                            'status': str(ev.get('status') or ''),
                            'elapsed_ms': int(ev.get('elapsed_ms') or 0),
                            'result_code': str(ev.get('result_code') or '')}}


def _citations_wire(result: dict) -> tuple[list, int | None]:
    """evidence_check → citations[{handle,state}]（state ∈ accepted/rejected）+ 隐藏计数。"""
    ec = (result or {}).get('evidence_check') or {}
    out = [{'handle': str(h), 'state': 'accepted'} for h in (ec.get('accepted') or [])]
    out += [{'handle': str(h), 'state': 'rejected'} for h in (ec.get('rejected') or [])]
    hidden = ec.get('hidden_in_answer')
    return out, (int(hidden) if isinstance(hidden, (int, float)) else None)


def _result_status(final: dict, result: dict) -> str:
    """succeeded→交付状态或 completion_note；failed→失败路径码（诚实分类，不掩盖）。"""
    if final.get('status') == 'succeeded':
        delivery_state = str((result or {}).get('delivery_state') or '')
        if delivery_state and delivery_state != 'normal':
            return delivery_state
        return str((result or {}).get('completion_note') or 'no_final_answer')
    if final.get('truncated'):
        return 'truncated_no_final_answer'
    err = final.get('error') or {}
    stage, code = str(err.get('stage') or ''), str(err.get('code') or '')
    return '%s:%s' % (stage, code) if stage else (code or 'graph_error')


def has_business_answer(result: dict) -> bool:
    """装配层判据（`result_json.completion_note`）→ 业务正文存在性（bool，不重算判据）。"""
    return str((result or {}).get('completion_note') or '') == 'has_final_answer'


# 实际查询范围来自成功查询台账，不从读数反推成员。
# 按指标保留调查对象，比较集合与筛选范围分别展示；没有成功查询时不生成范围摘要。
QUERY_SCOPE_KEYS = ('queries_n', 'investigation_product_ids', 'investigation_product_n',
                    'per_metric', 'baseline_sets', 'filter_scopes')
_INVESTIGATION_OPS = ('value', 'diff', 'states')
_BASELINE_OPS = ('set_compare',)
_FILTER_OPS = ('cohort_filter',)


def query_scope_wire(query_scopes: list | None) -> dict | None:
    """运行期查询台账 → 页面「本轮实际调查范围与比较基准」投影；无成功查询 = None。"""
    entries = [e for e in (query_scopes or []) if isinstance(e, dict) and e.get('op')]
    if not entries:
        return None
    investigation: set[str] = set()
    per_metric: dict[str, set[str]] = {}
    baseline_sets: list[dict] = []
    filter_scopes: list[dict] = []
    for e in entries:
        op = str(e.get('op') or '')
        pids = [str(p) for p in (e.get('product_ids') or []) if str(p or '')]
        metrics = [str(m) for m in (e.get('metrics') or []) if str(m or '')]
        periods = [str(x) for x in (e.get('period_ids') or []) if str(x or '')]
        if op in _INVESTIGATION_OPS:
            investigation.update(pids)
            for m in metrics:
                per_metric.setdefault(m, set()).update(pids)
            if not metrics:                       # 无指标声明的查询：归入空指标键（不猜指标名）
                per_metric.setdefault('', set()).update(pids)
        elif op in _BASELINE_OPS:
            baseline_sets.append({'op': op, 'metric': (metrics[0] if metrics else ''),
                                  'period_ids': periods, 'product_ids': pids,
                                  'product_n': len(pids)})
        elif op in _FILTER_OPS:
            # 全池筛选请求没有显式商品 ID，分母必须取服务端 scope_count；不可得时保持 None。
            scope_ids = [str(p) for p in (e.get('scope_product_ids') or []) if str(p or '')]
            scope_n = e.get('scope_n')
            if not isinstance(scope_n, int) or isinstance(scope_n, bool):
                scope_n = len(scope_ids) if scope_ids else None
            filter_scopes.append({'op': op, 'scope_kind': str(e.get('scope_kind') or ''),
                                  'product_ids': scope_ids, 'product_n': scope_n})
    return {
        'queries_n': len(entries),
        'investigation_product_ids': sorted(investigation),
        'investigation_product_n': len(investigation),
        'per_metric': {m: sorted(ids) for m, ids in sorted(per_metric.items())},
        'baseline_sets': baseline_sets,
        'filter_scopes': filter_scopes,
    }


# 知识来源仅投影文档 ID、标题和类型，不下发正文、片段或相似度，不参与数值核验。
KNOWLEDGE_SOURCE_KEYS = ('doc_id', 'title', 'doc_type')
_KNOWLEDGE_DOC_TYPES = ('guide', 'case')


def knowledge_sources_wire(sources: dict | None) -> list[dict] | None:
    """运行期知识来源登记 → wire 列表（保登记顺序＝前端编号顺序）；空/畸形 = None。"""
    out: list[dict] = []
    for key, item in (sources or {}).items():
        if not isinstance(item, dict):
            continue
        doc_id = str(item.get('doc_id') or key or '').strip()
        if not doc_id:
            continue
        doc_type = str(item.get('doc_type') or '').strip()
        if doc_type not in _KNOWLEDGE_DOC_TYPES:
            doc_type = ''                      # 类型不可辨＝留空（不猜 guide/case）
        out.append({'doc_id': doc_id,
                    'title': str(item.get('title') or '').strip(),
                    'doc_type': doc_type})
    return out or None


REPORT_SCHEMA = 'oagent_report_v1'
REPORT_KEYS = ('schema', 'summary', 'key_findings', 'next_steps', 'caveats', 'detail_md')

ANSWER_LINE_MAX_CHARS = 120
# 独立短句的句末终止符（中文为主）：**只**在这些字符处收口。不含 ASCII '.'／',' ——小数点与
# 千分位会被误当句末，宁可判"无合适短句"也不切错位置。
_SENTENCE_TERMINATORS = '。！？!?…'

# 固定节标题：规范化（去 # 前缀/空白/括号注解）后与锚词**全等**才归类；同名节多次出现取第一节。
_REPORT_HEADINGS = {'结论': 'summary', '关键发现': 'key_findings',
                    '下一步': 'next_steps', '重要限制': 'caveats'}
_REPORT_HEADING_RE = re.compile(r'^\s{0,3}#{1,6}\s+(.*)$')
_REPORT_ANNOT_RE = re.compile(r'[（(][^）)]*[）)]')
_REPORT_ITEM_RE = re.compile(r'^\s*(?:[-*]|\d+[.)、])\s+(.*)$')


def _report_anchor(title: str) -> str | None:
    """标题去掉注解后须与合同字段名完全匹配，不做包含匹配。"""
    normalized = _REPORT_ANNOT_RE.sub('', title).strip()
    return _REPORT_HEADINGS.get(normalized)


def _report_list_items(body_lines: list[str]) -> list[str]:
    """按出现顺序提取列表项和散文段落，保留引用与占位符。

    紧邻列表项的续行合并到该条目；列表之外的文字按空行分段，不因存在列表而丢弃。"""
    items: list[str] = []
    cur: int | None = None        # 当前可续写的条目下标（None=无，空行即断开）
    for ln in body_lines:
        m = _REPORT_ITEM_RE.match(ln)
        if m and m.group(1).strip():
            items.append(m.group(1).strip())
            cur = len(items) - 1
            continue
        s = ln.strip()
        if not s:
            cur = None
            continue
        if cur is not None:
            items[cur] += '\n' + s
        else:
            items.append(s)
            cur = len(items) - 1
    return items


def build_report(visible_text: str) -> dict | None:
    """守卫后终答正文 → report v1；不满足最小形状（结论缺失）时返回 None（降级 answer_md）。"""
    text = str(visible_text or '')
    if not text.strip():
        return None
    summary_lines: list[str] = []
    lists: dict[str, list[str]] = {'key_findings': [], 'next_steps': [], 'caveats': []}
    cur: str | None = None       # 当前处于哪个合同节（None=非合同内容）
    seen: set[str] = set()       # 同名节取第一节：已登记的节再次出现，正文不并入
    dropped = False              # 当前行处于被丢弃的正文（重复节或节内嵌套子标题之下）
    for line in text.split('\n'):
        m = _REPORT_HEADING_RE.match(line)
        if m:
            field = _report_anchor(m.group(1))
            if field is not None and field not in seen:
                cur = field
                seen.add(field)
                dropped = False
            else:
                # 非合同标题（含同名节重复、节内子标题）：其下正文只留在 detail_md，
                # 不并入任何结构化字段——不猜归属（与隐藏占位同一保守纪律）。
                cur = None
                dropped = True
            continue
        if dropped or cur is None:
            continue
        if cur == 'summary':
            if line.strip():
                summary_lines.append(line.rstrip())
        else:
            lists[cur].append(line)
        # cur=None 的自由段不进字段（完整原文由 detail_md 承载）
    summary_text = '\n'.join(summary_lines).strip()
    if not summary_text:
        return None
    return {'schema': REPORT_SCHEMA, 'summary': summary_text,
            'key_findings': _report_list_items(lists['key_findings']),
            'next_steps': _report_list_items(lists['next_steps']),
            'caveats': _report_list_items(lists['caveats']),
            'detail_md': text}


def _leading_sentence(summary: str) -> str | None:
    """提取结论首行中以句末标点结束的短句，用作缺失 answer_line 的候选。

    不跨行拼接、不截断超长句，返回值必须是 summary 的真实前缀。"""
    s = summary.lstrip()
    for i, ch in enumerate(s):
        if ch == '\n':
            return None
        if ch in _SENTENCE_TERMINATORS:
            sentence = s[:i + 1].strip()
            if sentence and len(sentence) <= ANSWER_LINE_MAX_CHARS:
                return sentence
            return None
    return None


def report_with_answer_line(report: dict | None, answer_line: str | None,
                            intent_present: bool = False) -> dict | None:
    """将与结论正文一致的首屏短句加入报告。

    候选必须位于结论头部、满足长度限制并与处理后的正文逐字匹配，否则不交付。
    文字出处校验不证明经营判断正确；无报告或结论时原样返回。"""
    if not isinstance(report, dict) or not isinstance(report.get('summary'), str) \
            or not report['summary']:
        return report
    summary = report['summary']
    line = str(answer_line or '').strip()
    if line:
        # 字段存在：头位 + 长度 + 逐字三重门，任一不满足＝不交付（fail-closed，不兜底）。
        if len(line) > ANSWER_LINE_MAX_CHARS or not summary.lstrip().startswith(line) \
                or line not in summary:
            return report
        return dict(report, answer_line=line)
    # 字段缺失：仅在意图区确实存在时按结论首句兜底；无合适短句＝不并入该键。
    if intent_present:
        fallback = _leading_sentence(summary)
        if fallback:
            return dict(report, answer_line=fallback)
    return report


def _sse(event: dict) -> str:
    return 'data: %s\n\n' % json.dumps(event, ensure_ascii=False)


# 按当前运行的已核验引用回查登记证据，不重新查询或计算。
# 缺失与不可比状态原样保留；回查失败时不阻断终答，也不伪造原始读数。
# compare/catalog 引用仅展示来源实际包含的字段，不补出成员精确值。

DETAIL_LOCATE_RESOLVED = 'resolved'
DETAIL_LOCATE_MISSING = 'missing'
# value 子集品类合计引用类型（单点值见 pctx_query.AGGREGATE_SELECTED_TOTAL_PREFIX）；
# 页面详情面与完整品类合计共用「品类汇总」形状，本模块只做键形归一（不 import 数据层）。
PQ_SELECTED_TOTAL_KIND = 'category_selected_total'


def _detail_ref_kind(ref: str) -> tuple:
    """canonical 引用 → (query_id, kind)；解析不出返回 ('', '')。

    `pqev:<qid>:<kind>:...` kind ∈ period/object/state/diff/compare/compare_row/
    compare_missing/object_state/catalog/cohort/category_total/set_sum，以及日期窗口的
    window/window_object/window_diff/window_compare/window_compare_row/window_compare_missing/
    day_series。上述键形由 `pctx_query._result_qev_keys` 统一定义。
    `pev:<pid>:<period>` 取证引用 kind='evidence'。"""
    ref = str(ref or '')
    if ref.startswith('pev:'):
        return '', 'evidence'
    parts = ref.split(':')
    if len(parts) >= 3 and parts[0] == 'pqev':
        kind = parts[2]
        # value 子集合计与完整品类合计在页面详情面共用同一「品类汇总」形状。
        if kind == PQ_SELECTED_TOTAL_KIND:
            kind = 'category_total'
        return parts[1], kind
    return '', ''


def _detail_period(block) -> dict | None:
    """响应块 → 页面周期读数（只取真实存在字段的固定键位；无 period_id=非周期块）。"""
    if not isinstance(block, dict):
        return None
    pid = block.get('period_id')
    if not pid:
        return None
    return {'period_id': str(pid),
            'label': block.get('period_label'),
            'start_date': block.get('period_start_date'),
            'end_date': block.get('period_end_date'),
            'length_days': block.get('period_length_days', block.get('length_days')),
            'value': block.get('value'), 'unit': block.get('unit'),
            'state': block.get('state')}


def _detail_date_window(block) -> dict | None:
    """将证据中的真实日期窗口投影为页面周期读数。

    没有目录周期 ID 时，使用 start~end 表达时间身份；无法取得日期则返回 None。"""
    if not isinstance(block, dict):
        return None
    # 与 display_intent._window_identity 同一「三类持有者」读法：块本体（window_values /
    # window_diff 的 results 元素、base/target 侧）、`window` 键（window:/window_compare: 引用）、
    # `range` 键（day_series: 引用的窗口汇总块）；每类都可能是日级聚合块
    # （period_start_date/period_end_date）或窗口身份块（start_date/end_date）。
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
        return {'period_id': '%s~%s' % (start, end), 'label': None,
                'start_date': start, 'end_date': end, 'length_days': length,
                'value': cand.get('value'), 'unit': cand.get('unit'), 'state': cand.get('state')}
    return None


def _detail_denominator(block) -> dict | None:
    d = block.get('denominator') if isinstance(block, dict) else None
    if not isinstance(d, dict):
        return None
    return {'kind': d.get('kind'), 'value': d.get('value'), 'note': d.get('note')}


def evidence_details_wire(evidence_check: dict, resolve_resp: dict | None) -> list | None:
    """accepted 引用 × 登记回查结果 → evidence_details 列表；回查不可用=None（降级）。"""
    if not isinstance(resolve_resp, dict) or not resolve_resp.get('ok'):
        return None
    ec = evidence_check if isinstance(evidence_check, dict) else {}
    accepted = [str(r) for r in (ec.get('accepted') or [])]
    if not accepted:
        return []
    # handle 反查：guard.handle_resolutions（handle→canonical）中通过核验的 canonical→handle
    by_ref: dict[str, dict] = {}
    for item in resolve_resp.get('items') or []:
        if isinstance(item, dict) and item.get('ref'):
            by_ref[str(item['ref'])] = item
    handle_of: dict[str, str] = {}
    for handle, canonical in (ec.get('handle_resolutions') or {}).items():
        if canonical and str(canonical) in set(accepted):
            handle_of.setdefault(str(canonical), str(handle))
    return [_project_detail(ref, handle_of.get(ref, ''), by_ref.get(ref))
            for ref in accepted]


def _rank_detail_collection(content: dict) -> dict:
    """提取集合排名引用对应的可回查成员；非排名引用不改变详情形状。"""
    if content.get('kind') != 'rank':
        return {}
    objects = content.get('objects')
    ranking = content.get('ranking')
    missing_group = content.get('missing_group')
    return {
        'objects': list(objects) if isinstance(objects, list) else None,
        'ranking': (_pick_list(ranking, ('rank', 'product_id', 'value'))
                    if isinstance(ranking, list) else None),
        'missing_group': (_pick_list(missing_group,
                                     ('product_id', 'value', 'state', 'reason'))
                          if isinstance(missing_group, list) else None),
    }


def _group_membership(content: dict) -> dict:
    """分组行只投影已确认的 high/equal/low；缺失或未知值不猜。"""
    membership = {'group_member': True}
    group = content.get('group')
    if group in ('high', 'equal', 'low'):
        membership['group'] = group
    return membership


def _project_detail(ref: str, handle: str, resolved: dict | None) -> dict:
    """单条 accepted 引用 → 页面详情项（固定键位；无对应=全字段 None 的 missing 项）。"""
    qid, kind = _detail_ref_kind(ref)
    item = {'handle': handle, 'ref': ref, 'locate': DETAIL_LOCATE_MISSING,
            'source': 'period_query' if ref.startswith('pqev:')
            else ('single_period_evidence' if ref.startswith('pev:') else ''),
            'op': kind, 'query_id': qid,
            'metric': None, 'metric_name': None, 'unit': None,
            'product_id': None, 'product_name': None,
            'period': None, 'base': None, 'target': None,
            'value': None, 'state': None, 'reason': '',
            'change': None, 'denominator': None,
            'collection': None, 'membership': None,
            'metric_rows': None, 'states': None, 'series': None, 'note': ''}
    if resolved is None:
        item['reason'] = '回查响应缺少该引用条目（不猜内容）'
        return item
    if resolved.get('status') != DETAIL_LOCATE_RESOLVED or not isinstance(resolved.get('content'), dict):
        item['reason'] = str(resolved.get('reason') or '引用未在本根任务登记或定位不到')
        return item
    content = resolved['content']
    item['locate'] = DETAIL_LOCATE_RESOLVED
    if kind == 'set_sum':
        item.update({'metric': content.get('metric'), 'metric_name': content.get('metric_name'),
                     'unit': content.get('unit'),
                     'value': content.get('sum'),
                     'period': (_detail_date_window({'start_date': content.get('start_date'),
                                                     'end_date': content.get('end_date')})
                                or _detail_period({'period_id': content.get('period_id'),
                                                   'period_label': content.get('period_label')}))})
        states = content.get('non_comparable_by_state')
        item['collection'] = {
            'kind': 'set_sum', 'aggregate': content.get('aggregate'),
            'sum': content.get('sum'), 'complete': content.get('complete'),
            'sum_scope': content.get('sum_scope'),
            'selected_n': content.get('selected_n'),
            'comparable_n': content.get('comparable_n'),
            'non_comparable_n': content.get('non_comparable_n'),
            'non_comparable_by_state': (
                {str(k): int(v) for k, v in sorted(states.items())
                 if isinstance(v, int) and not isinstance(v, bool)}
                if isinstance(states, dict) else {}),
            'product_ids': ([str(pid) for pid in content.get('product_ids')]
                            if isinstance(content.get('product_ids'), list) else []),
        }
        item['note'] = str(content.get('summary_note') or '')
    elif kind == 'diff':
        item.update({'metric': content.get('metric'), 'metric_name': content.get('metric_name'),
                     'unit': content.get('unit'), 'product_id': str(content.get('product_id') or ''),
                     'period': None})
        item['base'] = _detail_period(content.get('base'))
        item['target'] = _detail_period(content.get('target'))
        item['change'] = {'absolute': content.get('absolute_diff'),
                          'percent': content.get('percent_diff'),
                          'direction': content.get('direction')}
        item['state'] = content.get('state')
        item['reason'] = str(content.get('unknown_reason') or '')
        item['note'] = str(content.get('state_note') or '')
    elif kind == 'object':
        item.update({'metric': content.get('metric'), 'metric_name': content.get('metric_name'),
                     'unit': content.get('unit'),
                     'product_id': str(content.get('product_id') or ''),
                     'product_name': content.get('product_name'),
                     'period': _detail_period(content),
                     'value': content.get('value'), 'state': content.get('state'),
                     'denominator': _detail_denominator(content)})
        item['reason'] = str(content.get('unknown_reason') or '')
    # 日期窗口证据使用真实起止日期作为时间身份。
    elif kind == 'window':
        # 窗口级引用：支持"该日期窗口被本次查询返回"，不含单对象数值
        item['period'] = _detail_date_window(content.get('window') or {})
        item['note'] = ('日期窗口引用：本次查询覆盖该起止日期区间（%s）'
                        % str(content.get('event') or '日期窗口查询'))
    elif kind == 'window_object':
        item.update({'metric': content.get('metric'), 'metric_name': content.get('metric_name'),
                     'unit': content.get('unit'),
                     'product_id': str(content.get('product_id') or ''),
                     'product_name': content.get('product_name'),
                     'period': _detail_date_window(content),
                     'value': content.get('value'), 'state': content.get('state'),
                     'denominator': _detail_denominator(content)})
        item['reason'] = str(content.get('unknown_reason') or '')
        item['note'] = ('日期窗口单期读数：该值为所标起止日期区间内的聚合读数（粒度 %s）'
                        % content['granularity'] if content.get('granularity')
                        else '日期窗口单期读数')
    elif kind == 'window_diff':
        item.update({'metric': content.get('metric'), 'metric_name': content.get('metric_name'),
                     'unit': content.get('unit'), 'product_id': str(content.get('product_id') or ''),
                     'period': None})
        item['base'] = _detail_date_window(content.get('base'))
        item['target'] = _detail_date_window(content.get('target'))
        item['change'] = {'absolute': content.get('absolute_diff'),
                          'percent': content.get('percent_diff'),
                          'direction': content.get('direction')}
        item['state'] = content.get('state')
        item['reason'] = str(content.get('unknown_reason') or '')
        item['note'] = str(content.get('state_note') or content.get('window_alignment_note') or '')
    elif kind == 'window_compare':
        high = content.get('high_group') or []
        equal = content.get('equal_group') or []
        low = content.get('low_group') or []
        ranking = content.get('ranking') or []
        item.update({'metric': content.get('metric'), 'metric_name': content.get('metric_name'),
                     'unit': content.get('unit'),
                     'period': _detail_date_window(content.get('window') or {})})
        item['collection'] = {'kind': content.get('kind'), 'median': content.get('median'),
                              'median_state': content.get('median_state'),
                              'selected_n': len(content.get('objects') or []),
                              'usable_n': content.get('usable_count'),
                              'missing_n': content.get('missing_count'),
                              'high_n': len(high), 'equal_n': len(equal), 'low_n': len(low),
                              'ranking_n': len(ranking),
                              **_rank_detail_collection(content)}
        item['state'] = content.get('median_state')
        item['note'] = str(content.get('median_rule') or content.get('benchmark_disclosure') or '')
    elif kind == 'window_compare_row':
        item['product_id'] = str(content.get('product_id') or '')
        if content.get('group_member'):
            item['membership'] = _group_membership(content)
            item.update({'metric': content.get('metric'),
                         'metric_name': content.get('metric_name'),
                         'unit': content.get('unit')})
            item['note'] = str(content.get('note') or '窗口集合比较分组成员')
        else:
            item['membership'] = {'rank': content.get('rank'), 'value': content.get('value')}
            item['value'] = content.get('value')
    elif kind == 'window_compare_missing':
        item.update({'product_id': str(content.get('product_id') or ''),
                     'value': content.get('value'), 'state': content.get('state')})
        item['reason'] = str(content.get('reason') or '')
    elif kind == 'day_series':
        rng = content.get('range') if isinstance(content.get('range'), dict) else {}
        item.update({'metric': content.get('metric'), 'metric_name': content.get('metric_name'),
                     'unit': rng.get('unit'), 'product_id': str(content.get('product_id') or ''),
                     'period': _detail_date_window(rng),
                     'value': rng.get('value'), 'state': rng.get('state')})
        days = []
        for d in (content.get('days') or []):
            if not isinstance(d, dict):
                continue
            days.append({'date': d.get('date'), 'value': d.get('value'),
                         'state': d.get('state'), 'reason': str(d.get('unknown_reason') or '')})
        item['series'] = days or None
        item['note'] = ('连续日读数：逐日值与窗口汇总同源（粒度 %s）'
                        % content['granularity'] if content.get('granularity')
                        else '连续日读数：逐日值与窗口汇总同源')
    elif kind == 'period':
        item['period'] = _detail_period(content)
        item['note'] = '周期级引用：支持"该周期被本次查询返回"，不含单对象数值'
    elif kind == 'state':
        item.update({'product_id': str(content.get('product_id') or ''),
                     'period': _detail_period(content), 'state': content.get('state')})
    elif kind == 'compare':
        high = content.get('high_group') or []
        equal = content.get('equal_group') or []
        low = content.get('low_group') or []
        ranking = content.get('ranking') or []
        item.update({'metric': content.get('metric'), 'metric_name': content.get('metric_name'),
                     'unit': content.get('unit'), 'period': _detail_period(content.get('period'))})
        coll = {'kind': content.get('kind'), 'median': content.get('median'),
                'median_state': content.get('median_state'),
                'selected_n': len(content.get('objects') or []),
                'usable_n': content.get('usable_count'), 'missing_n': content.get('missing_count'),
                'high_n': len(high), 'equal_n': len(equal), 'low_n': len(low),
                'ranking_n': len(ranking), **_rank_detail_collection(content)}
        item['collection'] = coll
        item['state'] = content.get('median_state')
        item['note'] = str(content.get('median_rule') or content.get('benchmark_disclosure') or '')
    elif kind == 'compare_row':
        item['product_id'] = str(content.get('product_id') or '')
        if content.get('group_member'):
            # 分组按商品 ID 字符串返回：成员身份是集合事实，精确行值不随该引用返回（不假造）
            item['membership'] = _group_membership(content)
            item.update({'metric': content.get('metric'),
                         'metric_name': content.get('metric_name'),
                         'unit': content.get('unit')})
            item['note'] = str(content.get('note') or '集合比较分组成员')
        else:
            item['membership'] = {'rank': content.get('rank'), 'value': content.get('value')}
            item['value'] = content.get('value')
    elif kind == 'compare_missing':
        item.update({'product_id': str(content.get('product_id') or ''),
                     'value': content.get('value'), 'state': content.get('state')})
        item['reason'] = str(content.get('reason') or '')
    elif kind == 'object_state':
        identity = content.get('identity') if isinstance(content.get('identity'), dict) else {}
        name = identity.get('product_name')
        item.update({'product_id': str(content.get('product_id') or ''),
                     'product_name': name if isinstance(name, str) else None,
                     'state': identity.get('state'),
                     'states': [str(s) for s in (content.get('states') or [])],
                     'base': _state_window_side(content, 'base'),
                     'target': _state_window_side(content, 'target')})
    elif kind == 'cohort':
        # 筛选集合的成员与计数属于登记证据，完整投影，不套用 compare/catalog 的成员限制。
        scope = content.get('scope') if isinstance(content.get('scope'), dict) else {}
        match_ids = [str(m.get('product_id')) for m in (content.get('matches') or [])
                     if isinstance(m, dict) and m.get('product_id')]
        unknown_members = []
        for u in (content.get('unknown') or []):
            if not isinstance(u, dict) or not u.get('product_id'):
                continue
            reasons = sorted({t for v in u.values() for t in unknown_reason_tokens(v)})
            unknown_members.append({'product_id': str(u['product_id']), 'reasons': reasons})
        item['collection'] = {'kind': 'cohort', 'scope_kind': scope.get('kind'),
                              'time_mode': content.get('time_mode'),
                              'scope_count': content.get('scope_count'),
                              'match_count': content.get('match_count'),
                              'nonmatch_count': content.get('nonmatch_count'),
                              'predicate_unknown_count': content.get('predicate_unknown_count'),
                              'confirmed_share_percent': content.get('confirmed_share_percent'),
                              'share_denominator': content.get('share_denominator'),
                              'members_complete': content.get('members_complete'),
                              'ordered_by': content.get('ordered_by'),
                              'conditions': content.get('conditions'),
                              'match_ids': match_ids, 'unknown_members': unknown_members}
        item['note'] = ('集合级筛选引用：条件、期间、全部命中与未知成员、计数随本引用'
                        '整体登记（成员全量，不截断）')
        if content.get('time_mode') == 'single_period':
            item['period'] = _detail_period({'period_id': content.get('period_id')})
        else:
            item['base'] = _detail_period({'period_id': content.get('base_period_id')})
            item['target'] = _detail_period({'period_id': content.get('target_period_id')})
    elif kind == 'catalog':
        item['collection'] = {'kind': 'catalog', 'view_version': content.get('view_version'),
                              'product_n': len(content.get('objects') or []),
                              'period_n': len(content.get('periods') or [])}
        item['note'] = str(content.get('demo_caliber') or '')
    elif kind == 'category_total':
        # 品类合计与覆盖计数直接取自登记响应，不复算；时间身份优先使用真实日期窗口。
        if str(content.get('shape') or '') == 'value':
            item.update({'metric': content.get('metric'),
                         'metric_name': content.get('metric_name'),
                         'unit': content.get('unit'),
                         'value': content.get('sum'),
                         'period': (_detail_period(content.get('period') or {})
                                    or _detail_date_window(content.get('window') or {}))})
            states = content.get('non_comparable_by_state')
            contributors = content.get('contributors')
            item['collection'] = {
                'kind': 'category_total',
                'aggregate_kind': content.get('aggregate_kind'),
                'category': content.get('category'),
                'group_by': content.get('group_by'),
                'member_scope': content.get('member_scope'),
                'catalog_member_count': content.get('catalog_member_count'),
                'member_count': content.get('member_count'),
                'coverage_complete': content.get('coverage_complete'),
                'sum': content.get('sum'),
                'complete': content.get('complete'),
                'selected_n': content.get('selected_n'),
                'comparable_n': content.get('comparable_n'),
                'non_comparable_n': content.get('non_comparable_n'),
                'non_comparable_by_state': (
                    {str(k): int(v) for k, v in sorted(states.items())
                     if isinstance(v, int) and not isinstance(v, bool)}
                    if isinstance(states, dict) else {}),
                'contributors': ([{'product_id': str(c.get('product_id')),
                                   'value': c.get('value'),
                                   'share_percent': c.get('share_percent')}
                                  for c in contributors if isinstance(c, dict)]
                                 if isinstance(contributors, list) else []),
            }
            item['note'] = ('品类合计引用：数值与覆盖取 Data 的同行读数；子集合计不等于完整品类'
                            '总量，贡献度只在该品类分母完整有效时给出。')
            return item
        item.update({'metric': content.get('metric'), 'metric_name': content.get('metric_name'),
                     'unit': content.get('unit'),
                     'base': (_detail_date_window(content.get('base_window') or {})
                              or _detail_period({'period_id': content.get('base_period_id')})),
                     'target': (_detail_date_window(content.get('target_window') or {})
                                or _detail_period({'period_id': content.get('target_period_id')}))})
        coverage = content.get('coverage') if isinstance(content.get('coverage'), dict) else {}
        by_state = coverage.get('non_comparable_by_state')
        item['collection'] = {
            'kind': 'category_total', 'category': content.get('category'),
            'group_by': content.get('group_by'), 'sum_scope': content.get('sum_scope'),
            'base_sum': content.get('base_sum'), 'target_sum': content.get('target_sum'),
            'absolute_change': content.get('absolute_change'),
            'percent_change': content.get('percent_change'),
            'selected_n': coverage.get('selected_product_count'),
            'comparable_n': coverage.get('comparable_product_count'),
            'non_comparable_n': coverage.get('non_comparable_product_count'),
            'non_comparable_by_state': (
                {str(k): v for k, v in by_state.items()}
                if isinstance(by_state, dict) else {}),
        }
        item['note'] = ('品类汇总引用：Data 已计算的品类合计，只含两期均可比的商品'
                        '（覆盖与不可比分类如上）；不代表整个品类的经营结果')
    elif kind == 'evidence':
        obj = content.get('object') if isinstance(content.get('object'), dict) else {}
        item.update({'product_id': str(obj.get('product_id') or ''),
                     'product_name': obj.get('product_name'),
                     'period': _detail_period(content.get('period'))})
        rows = []
        for block in (content.get('metric_values') or []):
            if isinstance(block, dict):
                rows.append({'metric': block.get('metric'), 'metric_name': block.get('metric_name'),
                             'unit': block.get('unit'), 'value': block.get('value'),
                             'state': block.get('state')})
        item['metric_rows'] = rows or None
        disclosure = content.get('state_disclosure') if isinstance(content.get('state_disclosure'), dict) else {}
        if disclosure.get('record_conflicts'):
            item['state'] = 'record_conflict'
            item['note'] = '本周期存在记录冲突披露（见取证登记响应）'
    else:
        item['reason'] = '未识别的引用键形（不猜测内容形状）'
    return item


def _state_window_side(content: dict, side: str) -> dict | None:
    """states 行 comparison_window + state_changes/attribute_drift → 该侧变化事实（无则 None）。"""
    window = content.get('comparison_window') if isinstance(content.get('comparison_window'), dict) else {}
    pid = window.get('%s_period_id' % side)
    if not pid:
        return None
    changes = []
    for ch in (content.get('state_changes') or []) + (content.get('attribute_drift') or []):
        if isinstance(ch, dict) and ch.get('%s_period_id' % side):
            changes.append({'field': ch.get('field'), 'value': ch.get('%s_value' % side)})
    return {'period_id': str(pid), 'window_state': window.get('state'),
            'changes': changes or None}


DISPLAY_BLOCKS_SCHEMA = 'oagent_display_blocks_v1'
DISPLAY_BLOCKS_KEYS = ('schema', 'run_id', 'parse_status', 'blocks')
DISPLAY_BLOCK_KEYS = ('index', 'type', 'display', 'verified_facts', 'recomputed', 'cohort',
                      'verification', 'data_issues')
_DISPLAY_FACT_KEYS = ('handle', 'ref', 'kind', 'metric', 'product_id', 'period_id', 'value',
                      'unit', 'state', 'base', 'target', 'absolute', 'percent', 'direction',
                      # 日期窗口与逐日读数从核验事实原样投影，缺值不代零。
                      'window', 'day_series',
                      # 单期集合合计引用（`kind='set_sum'`）的核验事实——数值、成员、覆盖计数、
                      # 状态分类与时间身份等原样取自 Data 的 single_period_totals 条目。
                      'single_period_totals')
_DISPLAY_DISPLAY_KEYS = ('subjects', 'metrics', 'periods')
_DISPLAY_GROUP_KEYS = ('metric', 'period_id', 'n', 'median', 'basis', 'members', 'excluded')
_DISPLAY_MEMBER_KEYS = ('handle', 'product_id', 'value')
_DISPLAY_EXCLUDED_KEYS = ('handle', 'product_id', 'state')
_DISPLAY_DIRS_KEYS = ('up', 'down', 'flat', 'n', 'basis', 'other')
_DISPLAY_OTHER_KEYS = ('handle', 'state', 'note')
_DISPLAY_RANK_KEYS = ('product_id', 'handle', 'metric', 'period_id', 'value', 'set_n',
                      'rank_desc', 'rank_asc', 'basis')
_DISPLAY_TOOLMEDIAN_KEYS = ('metric', 'period_id', 'handle', 'ref', 'median', 'label',
                            'product_ids', 'unit')
# comparison 块的服务端复算分组合计（`recomputed.group_totals`，键面闭合单点；
# 判据 owner＝display_intent._recompute_group_totals，本表只做下发键面收口）。
# `category`（目录可信品类名，混装多类目时按此拆成多条）置首位；模型自由文本不在此键面。
_DISPLAY_GROUP_TOTAL_KEYS = ('category', 'metric', 'unit', 'base_period_id', 'target_period_id',
                             'subjects_n', 'comparable_n', 'non_comparable_n',
                             'non_comparable_by_state', 'base_sum', 'target_sum',
                             'absolute_change', 'percent_change', 'percent_change_state',
                             'sum_scope', 'basis')
# 集合块内的**工具权威整体排名**（`recomputed.rankings`，键面闭合单点；判据 owner＝
# `display_intent._tool_rankings`，本表只做下发键面收口）。名次/顺序/原值逐字来自 Data 的
# `ranking`；`selected_n`/`usable_n`/`missing_n` 与 `period_id`（目录周期 ID 或真实日期窗口
# 身份串）如实标注「本次选中集合」与真实时间。中位数引用不产条目——无排名引用不伪造榜单。
_DISPLAY_RANKING_KEYS = ('kind', 'kind_name', 'metric', 'unit', 'period_id', 'window',
                         'selected_n', 'usable_n', 'missing_n', 'product_ids',
                         'missing_group', 'handle', 'ref', 'rows')
_DISPLAY_RANKING_ROW_KEYS = ('rank', 'product_id', 'value')
_DISPLAY_RANKING_MISSING_KEYS = ('product_id', 'value', 'state', 'reason')
# 单期集合合计的引用核验面（`verified_facts` 内 `single_period_totals` 键位，键面闭合单点；
# 判据 owner＝display_intent._fact_from_content，本表只做下发键面收口）。数值/成员/覆盖/缺失分类/
# 时间身份均原样取自 Data 的 single_period_totals 条目，不重算、不代 0。
_DISPLAY_SINGLE_PERIOD_TOTALS_KEYS = ('metric', 'metric_name', 'unit', 'aggregate', 'sum',
                                       'complete', 'sum_scope', 'selected_n', 'comparable_n',
                                       'non_comparable_n', 'non_comparable_by_state',
                                       'product_ids', 'period_id', 'start_date',
                                       'end_date', 'window_identity', 'note')
_DISPLAY_REFS_KEYS = ('total', 'verified_n', 'unresolved')
# cohort_list 块的权威集合段键面（内部面与下发面同键集；下发前仍经闭合投影重建，
# 列表项逐层白名单）。绑定/完整性门/集合等值对账的判据 owner＝display_intent._verify_cohort_block。
# `scope_signature`＝范围身份指纹（Data 已核实的完整 scope 成员集，见 display_intent
# `_cohort_scope_signature`）：展示层判「同一权威集合」用，不从命中/未命中名单推定完整范围。
_COHORT_KEYS = ('ref', 'handle', 'query_id', 'binding', 'time_mode', 'period_id',
                'base_period_id', 'target_period_id', 'scope_kind', 'scope_count',
                'scope_signature', 'conditions', 'ordered_by', 'match_count', 'nonmatch_count',
                'predicate_unknown_count', 'confirmed_share_percent', 'share_denominator',
                'members_complete', 'match_rows', 'unknown_members', 'declared',
                'reconciliation')
_COHORT_COND_KEYS = ('metric', 'field', 'operator', 'value')
_COHORT_ROW_KEYS = ('product_id', 'readings')
_COHORT_READING_KEYS = ('metric', 'field', 'value')
_COHORT_UNKNOWN_KEYS = ('product_id', 'reasons')
_COHORT_DECLARED_KEYS = ('matches', 'match_count', 'scope_count')
_COHORT_RECON_KEYS = ('status', 'missing_ids', 'extra_ids', 'duplicate_ids',
                      'match_count_declared', 'match_count_actual',
                      'scope_count_declared', 'scope_count_actual')

_UNKNOWN_REASON_RE = re.compile(r'^unknown\((.+)\)$')


def unknown_reason_tokens(v) -> list:
    """状态串 'unknown(xxx)' → [xxx]；其余值（数值/普通串/None）→ []。

    单点供两处消费（display_intent 的未知成员 reasons、wire_contract 的 evidence_details
    cohort 投影），不建第二套原因解析。"""
    if isinstance(v, str):
        m = _UNKNOWN_REASON_RE.match(v.strip())
        if m:
            return [m.group(1)]
    return []


def _pick(d: dict, keys: tuple) -> dict:
    """固定键位重建（缺键补 None，未知键丢弃）——键面闭合的单点手法。"""
    return {k: d.get(k) for k in keys}


def _pick_list(v, keys: tuple) -> list:
    return [_pick(x, keys) for x in (v or []) if isinstance(x, dict)]


def display_blocks_wire(display_result: dict | None) -> dict | None:
    """内部核验结果 → `display_blocks` Wire 形状；非 dict/无合格块/全部畸形 = None（降级）。"""
    if not isinstance(display_result, dict):
        return None
    blocks = display_result.get('blocks')
    if not isinstance(blocks, list) or not blocks:
        return None
    out_blocks = [pb for pb in (_project_display_block(b) for b in blocks) if pb is not None]
    if not out_blocks:
        return None
    parse = display_result.get('parse') if isinstance(display_result.get('parse'), dict) else {}
    return {'schema': DISPLAY_BLOCKS_SCHEMA,
            'run_id': str(display_result.get('run_id') or ''),
            'parse_status': str(parse.get('status') or ''),
            'blocks': out_blocks}


def _project_cohort(c) -> dict | None:
    """cohort_list 块的权威集合段键面投影（None 透传＝未绑定/降级块不携带集合数据）。"""
    if not isinstance(c, dict):
        return None
    conds = [_pick(x, _COHORT_COND_KEYS)
             for x in (c.get('conditions') or []) if isinstance(x, dict)]
    rows = []
    for r in (c.get('match_rows') or []):
        if not isinstance(r, dict):
            continue
        pr = _pick(r, _COHORT_ROW_KEYS)
        pr['readings'] = [_pick(x, _COHORT_READING_KEYS)
                          for x in (r.get('readings') or []) if isinstance(x, dict)]
        rows.append(pr)
    unknowns = []
    for u in (c.get('unknown_members') or []):
        if not isinstance(u, dict):
            continue
        pu = _pick(u, _COHORT_UNKNOWN_KEYS)
        pu['reasons'] = [str(x) for x in (u.get('reasons') or [])]
        unknowns.append(pu)
    dec = c.get('declared') if isinstance(c.get('declared'), dict) else {}
    declared = _pick(dec, _COHORT_DECLARED_KEYS)
    declared['matches'] = ([str(x) for x in (dec.get('matches') or [])]
                           if isinstance(dec.get('matches'), list) else None)
    rec = c.get('reconciliation') if isinstance(c.get('reconciliation'), dict) else {}
    recon = _pick(rec, _COHORT_RECON_KEYS)
    for k in ('missing_ids', 'extra_ids', 'duplicate_ids'):
        recon[k] = ([str(x) for x in (rec.get(k) or [])]
                    if isinstance(rec.get(k), list) else [])
    out = _pick(c, _COHORT_KEYS)
    out.update({'conditions': conds, 'match_rows': rows, 'unknown_members': unknowns,
                'declared': declared, 'reconciliation': recon})
    return out


def _project_display_block(b) -> dict | None:
    """单块键面投影；非 dict＝丢弃（服务端内部面异常形状不外发）。"""
    if not isinstance(b, dict):
        return None
    display = b.get('display') if isinstance(b.get('display'), dict) else {}
    v = b.get('verification') if isinstance(b.get('verification'), dict) else {}
    refs = v.get('refs') if isinstance(v.get('refs'), dict) else {}
    rec = b.get('recomputed') if isinstance(b.get('recomputed'), dict) else {}
    dirs = rec.get('direction_counts')
    if isinstance(dirs, dict) and dirs:
        dirs = _pick(dirs, _DISPLAY_DIRS_KEYS)
        dirs['other'] = _pick_list(dirs.get('other'), _DISPLAY_OTHER_KEYS)
    groups = []
    for g in (rec.get('median_by_group') or []):
        if not isinstance(g, dict):
            continue
        pg = _pick(g, _DISPLAY_GROUP_KEYS)
        pg['members'] = _pick_list(pg.get('members'), _DISPLAY_MEMBER_KEYS)
        pg['excluded'] = _pick_list(pg.get('excluded'), _DISPLAY_EXCLUDED_KEYS)
        groups.append(pg)
    group_totals = []
    for t in (rec.get('group_totals') or []):
        if not isinstance(t, dict):
            continue
        pt = _pick(t, _DISPLAY_GROUP_TOTAL_KEYS)
        states = pt.get('non_comparable_by_state')
        pt['non_comparable_by_state'] = (
            {str(k): int(v) for k, v in sorted(states.items())
             if isinstance(v, int) and not isinstance(v, bool)}
            if isinstance(states, dict) else {})
        group_totals.append(pt)
    rankings = []
    for rk in (rec.get('rankings') or []):
        if not isinstance(rk, dict):
            continue
        prk = _pick(rk, _DISPLAY_RANKING_KEYS)
        prk['rows'] = _pick_list(rk.get('rows'), _DISPLAY_RANKING_ROW_KEYS)
        prk['missing_group'] = (_pick_list(rk.get('missing_group'), _DISPLAY_RANKING_MISSING_KEYS)
                                if isinstance(rk.get('missing_group'), list) else None)
        rankings.append(prk)
    # 单期集合合计事实（`single_period_totals`）逐条按下发键面重建，数值/成员/覆盖/状态/
    # 时间身份原样取自 Data；不做重算、不代 0（键面闭合单点，不另开投影结构）。
    verified_facts = _pick_list(b.get('verified_facts'), _DISPLAY_FACT_KEYS)
    for vf in verified_facts:
        spt = vf.get('single_period_totals')
        if isinstance(spt, dict):
            pspt = _pick(spt, _DISPLAY_SINGLE_PERIOD_TOTALS_KEYS)
            pspt['product_ids'] = [str(pid) for pid in pspt.get('product_ids', [])
                                   if isinstance(pid, str)]
            states = pspt.get('non_comparable_by_state')
            pspt['non_comparable_by_state'] = (
                {str(k): int(v) for k, v in sorted(states.items())
                 if isinstance(v, int) and not isinstance(v, bool)}
                if isinstance(states, dict) else {})
            vf['single_period_totals'] = pspt
    return {
        'index': b.get('index'),
        'type': str(b.get('type') or ''),
        'display': _pick(display, _DISPLAY_DISPLAY_KEYS),
        'verified_facts': verified_facts,
        'recomputed': {
            'median_by_group': groups,
            'direction_counts': dirs or {},
            'ranks': _pick_list(rec.get('ranks'), _DISPLAY_RANK_KEYS),
            'tool_returned': _pick_list(rec.get('tool_returned'), _DISPLAY_TOOLMEDIAN_KEYS),
            'group_totals': group_totals,
            'rankings': rankings,
        },
        # cohort_list 权威集合段（非 cohort 块/未绑定块=None，键位恒在）
        'cohort': _project_cohort(b.get('cohort')),
        'verification': {'refs': _pick(refs, _DISPLAY_REFS_KEYS)},
        # 真实引用/口径/计算问题的块级局部说明（不含 checks 不支持/文本未扫描/subjects
        # 缺焦点读数；不生成整块或整页状态）。
        'data_issues': [str(x) for x in (b.get('data_issues') or [])],
    }
