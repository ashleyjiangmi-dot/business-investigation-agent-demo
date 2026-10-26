# -*- coding: utf-8 -*-
"""清理可见答案中的过程叙述，保留经营数据与结论。

只移除明确的过程段、宣告句与标记，不改写业务正文。
带证据引用、标题或百分比的段落受到保护；开头宣告只处理前三段，不截断整块内容。
清洗后为空时交由调用方显示无业务正文状态，不回退展示原始推理。"""
from __future__ import annotations

import re

# 思维链标记。用拼接构造，避免本文件正文里出现字面标签（会被上游工具改写）。
# 公开给下游（如 SSE 禁词扫描）复用，故命名为 OPEN/CLOSE。
_THINK = 'th' + 'ink'
OPEN = '<' + _THINK + '>'
CLOSE = '</' + _THINK + '>'

# 成对出现的完整思考块：整体删除。
_THINK_PAIR_RE = re.compile(re.escape(OPEN) + r'.*?' + re.escape(CLOSE), re.S)

# 正文判据：命中任意一条即视为「正式答案的一部分」，不再向前删。
_CITATION_RE = re.compile(r'\[E\d|qev:|pev:|\bev:')
_HEADING_RE = re.compile(r'(?m)^\s{0,3}#{1,6}\s')
_PERCENT_RE = re.compile(r'\d\s*%|百分之')

# 中文自述式过程叙述（主语是「我/本题」，谓语是「回答/作答/整理/给出结论」这一类动作）。
_CN_META_RE = re.compile(
    r'(?:我|我们|本题|这个问题|这[个]?问题)'
    r'[^。\n]{0,40}?'
    r'(?:可以|就能|足够|充分|清楚|明确|掌握|获得|拿到|有).{0,20}?'
    r'(?:回答|作答|答复|结论|收束|组织|总结|整理|开始|给出)'
    r'|让我(?:整理|梳理|总结|组织|给出|来|确认)'
    # 「我已经获得所需数据。」这类以取数/收束自述收尾的句子（锚在句尾，正文里
    # "我们获得的数据如下…"这类带业务下文的句子不会被误判）。
    r'|(?:我|我们)(?:已经|已|现在)?(?:获得|拿到|掌握|收集|有)(?:了)?'
    r'[^。\n]{0,12}?(?:数据|信息|依据|证据|结果|材料)[。\.]?\s*$'
    r'|(?:数据|结果|证据|信息)(?:已经?|尚)?(?:足够|完整|齐备|充分)[^。\n]{0,6}?'
    r'(?:回答|作答|答复)[。\.]?\s*$'
    # 即刻作答宣告必须占满整段；后面还有实质判断时保留。
    r'|^我(?:来|这就|现在就|马上|将)[^。\n]{0,6}?(?:组织|整理|梳理|撰写|写|给出)'
    r'[^。\n]{0,8}?(?:最终)?(?:回答|答复|结论|作答)[。\.]?\s*$'
    r'|(?:无需|不需要|不用|不必)[^。\n]{0,12}?(?:调用|查询|检索)'
    r'|(?:直接|现在)(?:给出|组织|作答|回答|整理|总结)'
    r'|给出最终(?:答复|回答)'
    r'|我(?:完成|已经?完成|不再)'
    r'|预算(?:已)?(?:用|达).{0,6}(?:上限|6/6|\d/\d)'
    # 额度叙述只有在段首为额度宣告、段尾为收束自述时才整体移除，避免删除业务判断。
    r'|^(?:我需要注意)?预算还剩[^。\n]{0,12}?(?:可用|查询|取证)[\s\S]{0,60}?'
    r'(?:已经?足够|不需要更多|可以收束|不必)[。\.]?\s*$'
    r'|可不再调\s*knowledge'
    r'|要不要调用'
    r'|^(?:回答|作答|答复|分析)(?:如下|如下所示)[。\.]?\s*$'
    # 无主语的取数完成宣告。
    r'|(?:数据|结果|证据|信息)(?:已经)?(?:取得|获取|拿到|就绪)[。\.]?\s*$'
)

# 工具选择和收束自述按整句匹配；混合业务段不能因出现过程措辞而整段删除。
# 含数字或结论标记的句子受到保护，避免把业务边界误当过程说明。
_CN_PROC_SENT_RE = re.compile(
    r'^(?![^。\n]*(?:\d|无法|不足以|确认|表明|证明|说明|需要补))'
    r'(?:'
    #   (1) 路由/检索纪律的**工具选择自述**：必须说出「用哪条依据作答」才算过程句。
    r'(?:[^。\n]{0,24}?[，,])?(?:根据|按|依据)(?:路由|检索)纪律'
    r'[^。\n]{0,48}?(?:知识|检索|查询|调用|工具|作答|回答|答复|指标)[^。\n]{0,16}'
    #   (2) 「这属于…一般问题，用知识依据来回答。」——用哪种依据作答的自述。
    r'|(?:[^。\n]{0,24}?[，,])?(?:用|靠)(?:经营)?(?:知识|数据|检索|经验)'
    r'[^。\n]{0,8}?依据?来回答'
    # 取数必要性的自述只允许过程语气收尾。
    r'|(?:[^。\n]{0,24}?[，,])?(?:我|我们|所以|因此){0,2}'
    r'(?:不需要|无需|不用|不必)(?:再)?(?:取|查|调)(?:具体)?(?:商品|数据|指标)[^。\n]{0,12}'
    #   (4) 「已经回答了。」——收束自述，整句就这一句、不带业务尾巴。
    r'|(?:我|这)?(?:已经|已)(?:回答|作答|答复|答完)了'
    r')[。\.]?\s*$')

# 英文 scratchpad 的自述措辞（与 _latin_ratio 联合判定，单命中不足以触发）。
_EN_META_RE = re.compile(
    r'(?:let me|i (?:now|have|ve|will| ?need)|key facts|core answer|so the|note:'
    r'|synthes|sufficient|conclude|query attempts|evidence calls)', re.I)

# 开头取数或作答宣告按句末边界匹配，避免删除后面仍有业务判断的句子。
_LEAD_ACQ_SENT_RE = re.compile(
    r'^(?:我|我们)(?:已经|已|现在)?[^。\n]{0,28}?'
    r'(?:获得|拿到|掌握|收集|查|看|有)[^。\n]{0,28}?'
    r'(?:数据|信息|依据|证据|结果|材料|排名|清单|情况)'
    r'(?:[，,][^。\n]{0,20}?(?:回答|作答|答复|足够|充分))?[。\.]$')
_LEAD_DUMP_LINE_RE = re.compile(r'^(?:我|我们)已有[^。\n]{0,8}?(?:数据|信息|依据|证据|结果)[：:]$')
_LEAD_ANSWER_SENT_RE = re.compile(
    r'^(?:根据|依据|按照)[^。\n]{0,16}?(?:查询|所查|检索|查看|取数)[^。\n]{0,16}?'
    r'(?:回答|作答|答复)[^。\n]{0,10}[。\.]$')
_LEAD_SUBJECTLESS_ACQ_SENT_RE = re.compile(
    r'^(?:数据|结果|证据|信息|读数)(?:已经|已)?(?:取得|获取|拿到|就绪|齐全|齐备)[。\.]?$')
# 作答计划须整句匹配，带有后续业务判断时不移除。
_LEAD_PLAN_SENT_RE = re.compile(
    r'^我(?:需要|将|要|来|这就|现在就|马上)(?:先)?(?:给出|提供|输出|整理|梳理|撰写|写)'
    r'(?:一下|一下这|一版)?[^。\n]{0,24}?(?:框架|回答|答复|结论|答案|要点|大纲)[。\.]?$')
# 段首宣告独占一行时只剥该行，后续带引用的读数保留。
_LEAD_PROCESS_LINE_RE = re.compile(
    r'^(?:让我|我来|我这就|我现在)[^：:\n]{0,12}?(?:整理|梳理|汇总|列出|组织|撰写)'
    r'[^：:\n]{0,8}?(?:数据|读数|数值|证据|信息|结果|材料|清单)?[：:]\s*$')
# 问题复述按配对引号识别，避免把引号内的逗号误当句子边界。
_LEAD_QRESTATE_RE = re.compile(
    r'^(?:用户|题目|本题|问题)(?:问的是|问的|问的问题是)'
    r'(?:「[^」]{0,120}」|“[^”]{0,120}”|"[^"]{0,120}"|\'[^\']{0,120}\')'
    r'(?:的[^，,。\n]{0,8})?[，,。\.]?'
    r'|^(?:用户|题目|本题|问题)(?:问的是|问的)[^。\n]{0,120}?[。\.]')
# 额度用尽段须同时满足段首额度宣告、段尾收束与无引用条件，才整体移除。
_LEAD_QUOTA_DROP_RE = re.compile(
    r'^(?:我需要注意)?(?:查询|检索)?(?:额度|预算)(?:已经|已)?(?:用尽|耗尽|用完|打满|花完)'
    r'[\s\S]{0,160}?(?:即可|为准|足够|不多说了|可以收束)[。\.]?\s*$')
# 额度倒装自述按完整句匹配，不据此删除后续数据覆盖说明。
_LEAD_QUOTA_USED_UP_SENT_RE = re.compile(
    r'^(?:我|我们)?(?:已|已经)用满[^。\n]{0,12}?额度[。\.]$')
# 取值罗列头只有在整行独占且段内无引用时才移除。
_LEAD_DUMP_HEAD_LINE_RE = re.compile(
    r'^(?:我|我们)(?:已有|有)[^。\n]{0,16}?(?:数据|信息|依据|证据|结果|读数)'
    r'[（(]?[^：:\n]{0,40}?[：:]\s*$')
# 作答动作宣告只匹配谈论回答过程的完整句，不匹配业务边界说明。
_LEAD_ANSWER_PLAN_SENT_RE = re.compile(
    r'^(?:我|我们)(?:要|需要|得|会|将|马上|这就)[^。\n]{0,16}?'
    r'(?:在回答里|在答案里|在回复里)[^。\n]{0,24}[。\.]$')
# 无主语的自我确认可移除；带业务对象和具体判断的确认句保留。
_LEAD_SELFCONFIRM_SENT_RE = re.compile(
    r'^(?:已|再|先)?(?:确认|核实)(?:真是|确实|属实|无误|没问题)[^。\n]{0,24}[。\.]$')
#   (5) **无主语作答宣告**（「回答用户。」）：整句就是"回答谁"，不含任何对象或读数。
_LEAD_ANSWER_TARGET_SENT_RE = re.compile(r'^(?:回答|答复)(?:用户|客户|他|她)?[。\.]$')


def _strip_lead_sentences(body: str) -> str:
    """最多三轮移除开头连续的完整宣告句，遇到不匹配即停止。

    只在当前首行内寻找句末标点，不将后续数据行并入待删除句子。"""
    for _ in range(3):
        first = re.match(r'^\s*([^。\n]*[。\.])\s*(.*)$', body, re.S)
        if not first:
            break
        sent, rest = first.group(1).strip(), first.group(2).strip()
        if _LEAD_ACQ_SENT_RE.match(sent) or _LEAD_ANSWER_SENT_RE.match(sent) \
                or _LEAD_SUBJECTLESS_ACQ_SENT_RE.match(sent) or _LEAD_PLAN_SENT_RE.match(sent) \
                or _CN_PROC_SENT_RE.match(sent) \
                or _LEAD_QUOTA_USED_UP_SENT_RE.match(sent) \
                or _LEAD_ANSWER_PLAN_SENT_RE.match(sent) \
                or _LEAD_SELFCONFIRM_SENT_RE.match(sent) \
                or _LEAD_ANSWER_TARGET_SENT_RE.match(sent):
            body = rest
            continue
        break
    return body


def _strip_lead_announcement(para: str) -> str:
    """只移除开头段中的宣告句、宣告行或前缀，保留段内其余内容。

    不截断到结论标题，因为标题前可能包含数据表与必要计算。"""
    body = para.strip()
    if not body:
        return para
    # 能整段判为过程叙述时不先剥首句，避免留下半句宣告。
    if _is_reasoning_para(body):
        return para
    lines = body.split('\n')
    # 宣告行独占段首且段内无引用时，整段裸值罗列不展示。
    if _LEAD_DUMP_LINE_RE.match(lines[0].strip()) and not _CITATION_RE.search(body):
        return ''
    # 段首独立宣告行只剥该行，后续引用读数保留。
    if len(lines) > 1 and _LEAD_PROCESS_LINE_RE.match(lines[0].strip()):
        return '\n'.join(lines[1:]).strip()
    # 按配对引号剥离问题复述，后续依据与判断保持原样。
    qr = _LEAD_QRESTATE_RE.match(body)
    if qr:
        return body[qr.end():].strip()
    body = _strip_lead_sentences(body)
    # 移除前置宣告后再次检查取值罗列头；带引用的数据仍受保护。
    if body and _LEAD_DUMP_HEAD_LINE_RE.match(body.strip()) and not _CITATION_RE.search(body):
        return ''
    # 剥离首句后再判断剩余内容是否完全为额度与收束自述，保留有引用的数据段。
    if body and _LEAD_QUOTA_DROP_RE.match(body) and not _CITATION_RE.search(body):
        return ''
    # 剥离后为空表示整段均为过程叙述，不能回填原文。
    return body


# 取首个标题或结论式开头中更早的位置，保留主题标题并兼容无标题的结论开头。
_CONCLUSION_ANCHOR_RE = re.compile(
    r'(?m)^\s{0,3}(?:#{1,6}\s*[^\n]*结论|\*\*\s*结论|\*\*\s*核心判断)')


def _latin_ratio(text: str) -> float:
    """拉丁字符在「拉丁+汉字」中的占比；两者都没有时返回 0（不据此判英文）。"""
    latin = len(re.findall(r'[A-Za-z]', text))
    cjk = len(re.findall(r'[一-鿿]', text))
    if not (latin + cjk):
        return 0.0
    return latin / (latin + cjk)


def _sentences(text: str) -> list[str]:
    """按句切段（与 `_strip_lead_sentences` 同一口径：切到第一个句号为止；没有句号的行独立成句）。"""
    out, rest = [], text.strip()
    while rest:
        m = re.match(r'^([^。\n]*[。\.])\s*(.*)$', rest, re.S)
        if m:
            out.append(m.group(1).strip())
            rest = m.group(2).strip()
            continue
        head, _, tail = rest.partition('\n')
        if head.strip():
            out.append(head.strip())
        rest = tail.strip()
    return out


def _is_process_sentence(sent: str) -> bool:
    """使用统一的整句判据识别取数自述、作答计划与过程宣告，供段落和段尾清理共用。"""
    return bool(_CN_PROC_SENT_RE.match(sent)
                or _LEAD_ACQ_SENT_RE.match(sent) or _LEAD_ANSWER_SENT_RE.match(sent)
                or _LEAD_SUBJECTLESS_ACQ_SENT_RE.match(sent) or _LEAD_PLAN_SENT_RE.match(sent)
                or _LEAD_QUOTA_USED_UP_SENT_RE.match(sent)
                or _LEAD_ANSWER_PLAN_SENT_RE.match(sent)
                or _LEAD_SELFCONFIRM_SENT_RE.match(sent)
                or _LEAD_ANSWER_TARGET_SENT_RE.match(sent))


def _all_sentences_process_announcement(para: str) -> bool:
    """仅当段内每句均为已识别的过程宣告时，才判定整段可移除。

    含业务句的混合段交由段尾清理处理，不以数字有无替代句型判断。"""
    sents = _sentences(para)
    return bool(sents) and all(_is_process_sentence(s) for s in sents)


def _drop_trailing_process_sentence(para: str) -> str:
    """段尾那句是过程宣告、但前面还有其他句子 ⇒ **只剥这一句**，前面的内容逐字留下。

    整段就那一句的情况不归这里管（那由段级判据整段判掉），所以这里硬性要求 `len(sents) >= 2`。
    """
    sents = _sentences(para)
    if len(sents) < 2 or not _is_process_sentence(sents[-1]):
        return para
    body = para.rstrip()
    cut = body.rfind(sents[-1])
    return body[:cut].strip() if cut > 0 else para


def _is_reasoning_para(para: str) -> bool:
    """单个段落是否属于「过程叙述/草稿」：先排除正文判据，再认中英两种自述。
    开头段与正文中夹杂段共用这一条判据。"""
    body = para.strip()
    if not body or body == '---':
        return True                                   # 空段与裸分隔线不单独展示
    if (_CITATION_RE.search(para) or _HEADING_RE.match(body) or _PERCENT_RE.search(para)):
        return False                                  # 带引用/标题/数值 ⇒ 正文，停
    ratio = _latin_ratio(para)
    if ratio >= 0.6 and (_EN_META_RE.search(para) or ratio >= 0.9):
        return True
    return bool(_CN_META_RE.search(para)) or _all_sentences_process_announcement(para)


def _paras(text: str) -> list[str]:
    return re.split(r'\n\s*\n', text)


def _drop_process_paras(text: str) -> str:
    """移除纯过程段，保留数据和正式结论；全部移除时返回空串。

    先用原段做完整判定，再对需保留的段落清理末尾宣告，避免改变分类依据。"""
    kept = []
    for p in _paras(text):
        if _is_reasoning_para(p):
            continue
        kept.append(_drop_trailing_process_sentence(p))
    return '\n\n'.join(kept).strip()


# 占位文案不能固定归因于模型只输出推理，因为该状态也可能由仅返回展示意图触发。
# 原始输出保留在后台，具体原因由调用方登记。
NO_DISPLAYABLE_ANSWER = ('（未生成可展示的最终回答：本轮模型输出未包含可交付的业务正文，'
                         '已按展示规则隐藏；原始输出见运行记录）')
# 对应的诊断态，写进既有 final_answer_branch_reason 槽位（不新增 result_json 键）。
BRANCH_REASONING_ONLY = 'reasoning_only_hidden'


def clean_final_answer(text: str) -> str:
    """终答 → 用户可见正文。纯函数、确定性、零模型调用；无可展示内容时返回空串。"""
    if not text or not text.strip():
        return text
    # 1) 思维链标记：成对块整体删；只剩孤立结束标签时（中转站吃掉开始标签），
    #    首个孤立结束标签之前按定义就是思考内容 ⇒ 只截到**首个**孤立 CLOSE。
    #    其后若还有孤立 CLOSE（两个开始标签都被吃掉），两者之间的内容按定义是正文
    #    ⇒ 只剥标签本身、不丢内容（截到最后一个会把两标签间的正文整段丢掉）。
    t = _THINK_PAIR_RE.sub('', text)
    if CLOSE in t:
        t = t[t.index(CLOSE) + len(CLOSE):]
    t = t.replace(OPEN, '').replace(CLOSE, '')
    # 英文自述开头只清理到首个标题或结论式开头，避免删除正式内容。
    paras = _paras(t)
    if paras and _latin_ratio(paras[0]) >= 0.6 and _EN_META_RE.search(paras[0]):
        starts = [m.start() for m in (_HEADING_RE.search(t), _CONCLUSION_ANCHOR_RE.search(t))
                  if m and m.start() > 0 and t[m.start():].strip()]
        if starts:
            t = t[min(starts):]
    # 只处理前三段的开头宣告；逐句移除，不整块截断，带引用的数据段保留。
    head_paras = _paras(t)
    seen = 0
    for i, p in enumerate(head_paras):
        if not p.strip():
            continue
        if seen >= 3:
            break
        seen += 1
        head_paras[i] = _strip_lead_announcement(p)
    if seen:
        t = '\n\n'.join(head_paras)
    # 3) 全文纯过程叙述段（含夹杂在正文中间的那些）。
    return _drop_process_paras(t)
