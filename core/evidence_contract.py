# -*- coding: utf-8 -*-
r"""终答引用提取、核验结果消费与正文投影。

区分格式错误与真实但无效的引用，保留审计原文；对无效引用所在单元作局部提示。
可见正文、引用状态和运行记录分别装配，避免把后台核验信息写入业务正文。"""
from __future__ import annotations

import json
import re

# 仅提取可送核验的引用；格式错误由正文清理处理，不赋予其已核验身份。
_REFS_RE = re.compile(r'\[E\d+\]|(?:pqev|pev|qev|ev):[A-Za-z0-9_:.\-]+')

_HIDDEN_REF = '〔无效引用，已隐藏〕'

_HIDDEN_PROBE_RE = re.compile(r'\[E[^\]]*\]')
# 纯格式错误与「真实句柄」的判别：`[E<数字>]` 才是可送核验的句柄形态；区间/列表/缺号
# 形态（`[E9-E13]`/`[E9,E13]`/`[E]` 等）是引用格式错误，直接从可见正文移除。
# 畸形判别边界：内形以 E 起头且只含 E/数字/空白/分隔符（区间/列表形态 `E9-E13`/`E9,E13`
# 含第二个 E）才算引用格式错误；含其它字母的 `[E...]`（如正文英文词 `[Excel]`）不是
# 引用尝试，原样保留、不计格式错误。
_CANON_HANDLE_RE = re.compile(r'E\d+')
_MALFORMED_INNER_RE = re.compile(r'^E[\dE\s,，、;；\-–—/~～到至]*$')
_CANON_RE = re.compile(r'(?:pqev|pev|qev|ev):[A-Za-z0-9_:.\-]+')

# 终答分支原因码（诊断；取值集合见 final_branch_reason）
BRANCH_LAST_ROUND_OK = 'last_round_extracted'
BRANCH_LAST_ROUND_HAS_TOOL_RESPONSES = 'last_round_has_tool_responses'
BRANCH_ROUNDS_SHAPE_UNUSABLE = 'rounds_shape_unusable'
BRANCH_LAST_ROUND_TEXT_EMPTY = 'last_round_text_empty'



def final_from_rounds(rounds, fallback):
    """提取末轮无工具调用的模型文本；截断或记录形状不符时使用累积文本。

    轮次数据可能被字符串化；无法可靠识别末轮时保留完整累积内容。"""
    try:
        if isinstance(rounds, str):
            rounds = json.loads(rounds) if rounds.strip() else None
        if isinstance(rounds, list) and rounds and isinstance(rounds[-1], dict):
            out = (rounds[-1].get('data') or {}).get('output') or {}
            if isinstance(out, dict) and not out.get('tool_responses'):
                last = str(out.get('llm_response') or '')
                if last.strip():
                    return last, 'last_round'
    except Exception:
        pass
    return fallback, 'accumulated_text'


def extract_refs(final_text: str) -> list:
    """引用提取（[E1]→裸 E1 交 guard 解析 canonical；去重保序；空集合法）。"""
    seen, refs = set(), []
    for m in _REFS_RE.finditer(str(final_text or '')):
        r = m.group(0)
        if r.startswith('[') and r.endswith(']'):
            r = r[1:-1]
        if r not in seen:
            seen.add(r)
            refs.append(r)
    return refs


def ref_extract_main(agent_text, run_id, agent_rounds_json=None) -> dict:
    """提取终答与引用，组装引用核验请求。"""
    raw = str(agent_text or '')
    final_text, final_source = final_from_rounds(agent_rounds_json, raw)
    refs = extract_refs(final_text)
    request = {'run_id': str(run_id or '__missing__'), 'refs': refs}
    return {'refs_json': json.dumps(refs, ensure_ascii=False),
            'request_json': json.dumps(request, ensure_ascii=False),
            'ref_count': str(len(refs)),
            'final_text': final_text,
            'final_source': final_source}



def _loads(s):
    try:
        return json.loads(str(s or '') or '')
    except Exception:
        return None


def _shallow_content(rec):
    """将专项记录中的嵌套对象与数组转为 JSON 字符串，限制输出对象深度。"""
    if not isinstance(rec, dict):
        return rec
    rec = dict(rec)
    ei = rec.get('evidence_index') or []
    if isinstance(ei, list):
        rec['evidence_index'] = []
        for it in ei:
            if not isinstance(it, dict):
                rec['evidence_index'].append(it)
                continue
            c = it.get('content')
            if isinstance(c, (dict, list)):
                it = dict(it)
                it['content'] = json.dumps(c, ensure_ascii=False)
            rec['evidence_index'].append(it)
    findings = rec.get('findings') or []
    if isinstance(findings, list):
        rec['findings'] = []
        for f in findings:
            if not isinstance(f, dict):
                rec['findings'].append(f)
                continue
            refs = f.get('evidence_refs')
            if isinstance(refs, (dict, list)):
                f = dict(f)
                f['evidence_refs'] = json.dumps(refs, ensure_ascii=False)
            rec['findings'].append(f)
    return rec


def _resolve_accepted_handles(guard):
    """guard.handle_resolutions（被引 handle→canonical）中通过核验的 handle 集合。"""
    if not isinstance(guard, dict):
        return set()
    accepted = set(guard.get('accepted') or [])
    resolutions = guard.get('handle_resolutions') or {}
    return {str(h) for h, c in resolutions.items() if c and str(c) in accepted}


def _redact_citations(text, guard):
    """从用户可见正文处理引用（确定性、fail-closed），并分开两类异常：

    - **纯格式错误**（`[E9-E13]`/`[E9,E13]`/`[E]` 等 `_HIDDEN_PROBE_RE` 命中但不是
      `[E<数字>]` 的形态，且内形只含 E/数字/分隔符）：不是可核验引用，直接从可见正文
      移除，不留占位、不触发 `evidence_unverified`（原文与计数仍保留在运行记录/
      `evidence_check`）。含其它字母的 `[E...]`（如英文词 `[Excel]`）不属引用尝试，
      原样保留。
      即使守卫不可用也可判定并移除——格式错误与核验服务无关。
    - **真实句柄但本轮未签发/被拒**（`[E99]` 等 canonical handle 不在已核验 handle 集）：
      隐藏为 `_HIDDEN_REF`，不冒充有效依据；由 `isolate_unverified_claims` 做局部提示。
    - canonical 引用 pqev:/pev:/qev:/ev:：命中 guard.rejected=隐藏（行为不变）。
    守卫服务未完成（guard=None）时无法判定 canonical 句柄，不隐藏（evidence_check 已
    如实登记未核验），但格式错误仍移除。
    返回（替换后正文, 隐藏处数, 移除的格式错误处数）。"""
    text = str(text or '')
    hidden = [0]
    malformed = [0]

    def _format_repl(m):
        token = m.group(0)
        inner = token[1:-1]
        if _CANON_HANDLE_RE.fullmatch(inner):
            return token                     # 真实句柄形态：留给 guard 判定
        if not _MALFORMED_INNER_RE.match(inner):
            return token                     # 含其它字母＝非引用尝试的正文：原样保留
        malformed[0] += 1
        return ''                            # 纯格式错误：静默移除，不占位

    if guard is None:
        return _HIDDEN_PROBE_RE.sub(_format_repl, text), 0, malformed[0]

    rejected = set(guard.get('rejected') or [])
    ok_handles = _resolve_accepted_handles(guard)

    def _handle_repl(m):
        token = m.group(0)
        inner = token[1:-1]
        if inner in ok_handles:
            return token
        if _CANON_HANDLE_RE.fullmatch(inner):
            hidden[0] += 1
            return _HIDDEN_REF
        if not _MALFORMED_INNER_RE.match(inner):
            return token                     # 含其它字母＝非引用尝试的正文：原样保留
        malformed[0] += 1
        return ''                            # 区间/列表等畸形形态：移除，不展开、不猜意图

    def _ref_repl(m):
        if m.group(0) in rejected:
            hidden[0] += 1
            return _HIDDEN_REF
        return m.group(0)

    text = _HIDDEN_PROBE_RE.sub(_handle_repl, text)
    text = _CANON_RE.sub(_ref_repl, text)
    return text, hidden[0], malformed[0]


def projection_main(agent_text: str, guard_body: str, refs_json: str, run_id: str,
                    entry_meta_json: str, selection_json: str, user_question: str,
                    specialist_records_json: str, final_text: str = '',
                    final_source: str = '', branch_reason: str = '',
                    has_business_answer: bool | None = None) -> dict:
    """装配业务正文与后台运行记录。

    无效引用从可见正文隐藏，原文保留在 raw_answer；守卫不可用时记录未核验状态。
    branch_reason 仅透传到后台字段，不进入正文。
    has_business_answer 应在清洗正文后、写入占位文案前计算；未提供时以原文非空判断完成状态。"""
    selection = _loads(selection_json) or {}
    guard = _loads(guard_body)
    refs = _loads(refs_json) or []
    raw_answer = str(agent_text or '')
    #  终答交付：final_text 由 ref_extract 按平台逐轮记录提取（last_round）或回退
    # 累积 text（accumulated_text，截断场景）；raw_answer 恒为平台累积原文留排查。
    _ft = str(final_text or '')
    if _ft.strip():
        answer_text, _src = _ft, (str(final_source or '') or 'last_round')
    else:
        answer_text, _src = raw_answer, 'accumulated_text'
    visible_answer, hidden_n, malformed_n = _redact_citations(answer_text, guard)
    if guard is None:
        evidence_check = {'ok': False, 'guard_unavailable': True,
                          'detail': '引用守卫服务未完成（响应缺失/非 JSON）：本轮引用未核验，'
                                    '数值结论仍以工具返回为准，不代 0、不冒充已核验',
                          'handle_resolutions': {}, 'hidden_in_answer': 0,
                          'format_errors_removed': malformed_n}
        root_budget = None
    else:
        evidence_check = {'ok': bool(guard.get('ok')), 'guard_unavailable': False,
                          'accepted': guard.get('accepted') or [],
                          'rejected': guard.get('rejected') or [],
                          'foreign_namespace': guard.get('foreign_namespace') or [],
                          'handle_resolutions': guard.get('handle_resolutions') or {},
                          'hidden_in_answer': hidden_n,
                          'format_errors_removed': malformed_n,
                          'detail': guard.get('note') or ''}
        root_budget = guard.get('root_budget')
    # 专项委派记录。specialist_records_json=真实 /oagent_specialist_status **完整响应**
    # {ok, run_id, records, root_budget, note}（HTTP body 原样）——先解析 status object 再取
    # records 数组；响应缺失/非 JSON/未 ok/records 非数组=显式 unavailable（不代 0、不冒充已登记）。
    status_obj = _loads(specialist_records_json)
    records = []
    if not isinstance(status_obj, dict) or not status_obj.get('ok'):
        specialist_status = {'unavailable': True,
                             'detail': '专项状态服务未完成（响应缺失/非 JSON/未 ok）：专项记录未呈现'}
    elif not isinstance(status_obj.get('records'), list):
        specialist_status = {'unavailable': True,
                             'detail': '专项状态 records 不是数组：专项记录未呈现'}
    else:
        records = [_shallow_content(r) for r in status_obj['records'] if isinstance(r, dict)]
        specialist_status = {'unavailable': False, 'records_n': len(records)}
    if has_business_answer is None:
        completion_note = 'has_final_answer' if raw_answer.strip() else 'no_final_answer'
    else:
        completion_note = 'has_final_answer' if has_business_answer else 'no_final_answer'
    result = {'final_answer': visible_answer,
              'raw_answer': raw_answer,
              'final_answer_source': _src,
              'final_answer_branch_reason': str(branch_reason or ''),
              'completion_note': completion_note,
              'evidence_check': evidence_check,
              'refs': refs,
              'root_budget': root_budget,
              'root_run_id': str(run_id or ''),
              'specialist_records': records,
              'specialist_status': specialist_status,
              'selection': selection,
              'entry_meta': _loads(entry_meta_json),
              'demo_note': '主 Agent 持续决策输出（Demo 口径）；证据核验与预算/专项记录为确定性登记'
                           '（预算与专项记录仅在本 JSON 后台字段，不进用户正文）；'
                           'rejected 引用不在 projection_md/final_answer 原样展示（raw_answer 保留原文）；'
                           'final_answer_source=last_round（平台逐轮记录末轮终答）或 accumulated_text'
                           '（截断回退累积原文）'}
    return {'projection_md': visible_answer, 'result_json': result}


# 对包含无效引用的最小完整单元追加局部提示，不改变原始核验结果。

# 提示符避开引用、标题和表格分隔字符，防止被后续解析器再次识别。
ISOLATION_MARKER = '〔局部提示：此处含未通过核验的引用，相关内容按未核验对待〕'

# 按段落与列表项划分提示范围，列表续行归入前一条目。
_BULLET_RE = re.compile(r'^\s*(?:[-*+]|\d+[.)、])\s+')
_HEADING_LINE_RE = re.compile(r'^\s{0,3}#{1,6}\s')   # 与 wire_contract._REPORT_HEADING_RE 同口径
_PARA_SPLIT_RE = re.compile(r'(\n\s*\n)')


def _marker_target_blocked(line: str) -> bool:
    """标记禁落行：ATX 标题行（前缀破坏 build_report 锚定、尾缀改标题字面）与 `---`
    分隔线（尾缀会让前端结论段的 `p !== '---'` 过滤失配）。"""
    return bool(_HEADING_LINE_RE.match(line)) or line.strip() == '---'


def _append_marker(line: str) -> str:
    """标记贴行尾；表格行（去尾空白后以 `|` 收口）标记进末格、保留尾 pipe 与列数
    （直接尾缀会切出幻影列）。"""
    stripped = line.rstrip()
    if stripped.endswith('|'):
        return (line[:len(stripped) - 1] + ISOLATION_MARKER
                + stripped[len(stripped) - 1:] + line[len(stripped):])
    return line + ISOLATION_MARKER


def _split_units(lines: list) -> list:
    """段内切单元：bullet 行起一个列表项单元，其后至下一 bullet 的非 bullet 行按懒延续归入
    同单元；首个 bullet 之前的连续非 bullet 行＝散文单元；无 bullet 的段＝单一散文单元。
    返回 [{'bullet': 首行下标|None, 'lines': [行下标…]}, …]（单元互不重叠、覆盖全段）。"""
    units: list = []
    cur: dict | None = None
    for i, ln in enumerate(lines):
        if _BULLET_RE.match(ln):
            if cur is not None:
                units.append(cur)
            cur = {'bullet': i, 'lines': [i]}
        elif cur is None:
            cur = {'bullet': None, 'lines': [i]}
        else:
            cur['lines'].append(i)
    if cur is not None:
        units.append(cur)
    return units


def isolate_unverified_claims(text) -> tuple:
    """Guard 后局部隔离：给含隐藏引用占位的最小完整单元追加用户局部提示。

    显式绑定判据＝单元文本含 `_HIDDEN_REF`（无效/隐藏/范围不符引用经 `_redact_citations`
    的统一落点＝现有字段与正文的唯一显式绑定点）；无占位的段落/列表项/独立事实逐字保留。
    不能证明句级映射时按受影响最小的完整段落或列表项隔离；不推断跨段语义、
    不新增依赖图、不要求 Guard 核验自然语言因果或计算解释。
    标记落点：列表项挂 bullet 首行行尾（前端 items 只取 bullet 行，续行会被丢）；散文挂
    单元内最后一个非禁落行行尾（段首可能为 `##` 标题行，前缀会破坏标题解析）；单元全为
    禁落行时标记在其后另起一行（单换行，不引入空行——paragraphs/build_report 的段界不变）。
    返回（改写后文本, 标记单元数）；无占位＝原样返回、计 0（含 guard=None 不隐藏路径）。
    """
    text = str(text or '')
    if not text or _HIDDEN_REF not in text:
        return text, 0
    parts = _PARA_SPLIT_RE.split(text)      # 奇数位＝段间分隔符，原样保留
    marked = 0
    for i in range(0, len(parts), 2):
        part = parts[i]
        if _HIDDEN_REF not in part:
            continue
        lines = part.split('\n')
        edits: list = []
        for u in _split_units(lines):
            if _HIDDEN_REF not in '\n'.join(lines[u['lines'][0]:u['lines'][-1] + 1]):
                continue                      # 单元无占位＝独立内容，零改动
            if u['bullet'] is not None:
                edits.append(('append', u['bullet']))
                continue
            tgt = next((j for j in reversed(u['lines'])
                        if not _marker_target_blocked(lines[j])), None)
            edits.append(('append', tgt) if tgt is not None
                         else ('insert_after', u['lines'][-1]))
        # 逆序套用：append 不移位；insert_after 只影响更靠后的下标（均已先处理）。
        for kind, idx in sorted(edits, key=lambda e: e[1], reverse=True):
            if kind == 'append':
                lines[idx] = _append_marker(lines[idx])
            else:
                lines.insert(idx + 1, ISOLATION_MARKER)
            marked += 1
        parts[i] = '\n'.join(lines)
    return ''.join(parts), marked


def platform_rounds_from_messages(messages: list) -> list:
    """按 assistant 消息及其后紧随的 tool 消息组装轮次；system/user 消息不单独构成轮次。"""
    rounds: list = []
    cur: dict | None = None
    for m in messages or []:
        role = m.get('role')
        if role == 'assistant':
            cur = {'data': {'output': {'llm_response': str(m.get('content') or ''),
                                       'tool_responses': []}}}
            rounds.append(cur)
        elif role == 'tool' and cur is not None:
            cur['data']['output']['tool_responses'].append(
                {'tool_call_id': m.get('tool_call_id'), 'content': str(m.get('content') or '')})
    return rounds


def accumulated_from_rounds(rounds: list) -> str:
    """按顺序拼接各轮模型文本。"""
    return ''.join(str(((r.get('data') or {}).get('output') or {}).get('llm_response') or '')
                   for r in (rounds or []) if isinstance(r, dict))


def final_branch_reason(rounds, final_source: str) -> str:
    """终答为何来自该分支的原因码（诊断；只读旁路，不改 final_from_rounds 的判据）。"""
    if str(final_source or '') == 'last_round':
        return BRANCH_LAST_ROUND_OK
    try:
        if isinstance(rounds, str):
            rounds = json.loads(rounds) if rounds.strip() else None
        if not (isinstance(rounds, list) and rounds and isinstance(rounds[-1], dict)):
            return BRANCH_ROUNDS_SHAPE_UNUSABLE
        out = (rounds[-1].get('data') or {}).get('output') or {}
        if not isinstance(out, dict):
            return BRANCH_ROUNDS_SHAPE_UNUSABLE
        if out.get('tool_responses'):
            return BRANCH_LAST_ROUND_HAS_TOOL_RESPONSES
        if not str(out.get('llm_response') or '').strip():
            return BRANCH_LAST_ROUND_TEXT_EMPTY
    except Exception:
        return BRANCH_ROUNDS_SHAPE_UNUSABLE
    return BRANCH_ROUNDS_SHAPE_UNUSABLE
