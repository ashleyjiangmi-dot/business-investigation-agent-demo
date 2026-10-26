# -*- coding: utf-8 -*-
"""经营调查入口适配：装配问题、范围、周期目录、业务口径与根任务输入。

数据与合同常量直接依赖 core.data，入口数据不可用时明确拒绝。"""
from __future__ import annotations

import json

from .data import oi03_brief as OB
from .data import operating_agent_brief as OAB
from .data import operating_catalog as OC

ENTRY_CONTRACT_VERSION = OAB.OAGENT_ENTRY_CONTRACT_VERSION
SHARED_NOTE_VERSION = OAB.OAGENT_SHARED_NOTE_VERSION
QUESTION_MAX_LENGTH = 500


def _period_time_key(p):
    """周期目录行的时间序键（日期区间为主，period_index 兜底；不按 ID 字符串判断）。"""
    return (str(p.get('period_start_date') or ''), str(p.get('period_end_date') or ''),
            str(p.get('period_index')))


def _reject(code, detail, meta):
    rejection = {'event': 'operating_agent_rejected', 'entry_stage': 'entry_adapter',
                 'error_code': code, 'detail': detail,
                 'demo_note': '入口合同拒绝（确定性，不猜测、不改写）：不进入主 Agent 决策。'}
    rejection.update(meta)
    return {'status': 'rejected', 'reject_code': code, 'reject_detail': detail,
            'rejection_json': rejection, 'run_id': '', 'ctx_json': '{}',
            'selected_product_ids_json': '[]',
            'benchmark_product_ids_json': '[]',
            'period_ids_json': '[]', 'selection_json': '{}', 'entry_meta_json': '{}',
            'agent_input_json': '', 'root_init_body': '', 'status_body': ''}


def main(user_question: str, product_scope: str,
         run_id: str, fetch_body: str,
         base_period_id: str = '', target_period_id: str = '') -> dict:
    """装配用户问题、调查范围与周期上下文。

目标期可附比较基期；未给基期时使用目标期的相邻可比期。
未给目标期时按题面日期或模拟业务日期解释；仅给基期则明确拒绝。
允许查询范围为服务端下发的完整周期目录，目标期不限制数据访问范围。"""
    q = str(user_question or '').strip()
    rid = str(run_id or '').strip()
    meta = {'user_question': q, 'product_scope': str(product_scope or ''),
            'base_period_id': str(base_period_id or ''),
            'target_period_id': str(target_period_id or '')}

    # 取上下文结果装载。失败即显式拒绝——无目录不下发、不猜测、不回退图内副本。
    parsed = None
    ctx_err = ''
    try:
        parsed = json.loads(str(fetch_body or '') or '')
    except Exception as e:
        ctx_err = '取上下文响应非合法 JSON：%s' % str(e)[:200]
    if isinstance(parsed, dict) and parsed.get('ok') and isinstance(parsed.get('catalog'), dict):
        catalog = parsed['catalog']
        proj = parsed.get('entry_projection') or {}
        products = catalog.get('products') or []
        periods = catalog.get('periods') or []
        versions = parsed.get('versions') or {}
        if not products or not periods or not isinstance(versions, dict) or not versions \
                or not isinstance(proj, dict) or not proj \
                or not str(parsed.get('shared_business_note') or '').strip():
            ctx_err = '取上下文响应缺目录/周期/入口选项投影/版本组/共享口径：显式拒绝（禁止回退图内副本）'
    else:
        catalog, products, periods, versions, proj = None, [], [], {}, {}
        if not ctx_err:
            ctx_err = '取上下文响应未 ok 或缺 catalog：显式拒绝（禁止回退图内副本）'
    if ctx_err:
        return _reject('ctx_fetch_failed', ctx_err, meta)

    if not rid:
        return _reject('run_id_missing',
                       'run_id 缺失（sys.workflow_run_id 未注入）：拒绝执行，不落共享桶', meta)
    if not q:
        return _reject('question_missing', '问题缺失：入口不使用默认问题掩盖输入错误', meta)
    if len(q) > QUESTION_MAX_LENGTH:
        return _reject('question_too_long', '问题超过 %d 字上限' % QUESTION_MAX_LENGTH, meta)

    # 同名商品必须使用带消歧信息的标签或商品 ID；只有名称唯一时才接受裸名称。
    by_label = {}
    name_counts = {}
    for p in products:
        nm = str(p.get('product_name') or '')
        name_counts[nm] = name_counts.get(nm, 0) + 1
    for p in products:
        pid = str(p['product_id'])
        nm = str(p.get('product_name') or '')
        by_label[str(p.get('scope_label') or nm)] = pid
        by_label[pid] = pid
        if nm and name_counts[nm] == 1:
            by_label[nm] = pid
    # 品类选择——标签→成员集合在同一份下发目录的 `category` 上现算（目录原始顺序、
    # 保留全部成员身份）；标签格式单点＝oi03_brief.category_scope_label，前端不另存品类目录。
    # 品类标签优先于同名单商品名判定：用户显式选品类＝本轮调查上界，不当单商品、不当全池。
    cat_ids = {}
    for p in products:
        cat = str(p.get('category') or '').strip()
        if cat:
            cat_ids.setdefault(OB.category_scope_label(cat), []).append(p['product_id'])
    all_ids = [p['product_id'] for p in products]
    all_label = str(proj.get('all_label') or '')
    if not all_label:
        return _reject('ctx_fetch_failed', '取上下文响应缺入口选项映射：显式拒绝（禁止回退图内副本）',
                       meta)

    # 仅给比较基期而未给目标期时拒绝请求，避免比较基准缺少归属期。
    target_sel = str(target_period_id or '').strip()
    base_sel = str(base_period_id or '').strip()
    no_period_context = False
    if not target_sel:
        if base_sel:
            return _reject('period_target_missing',
                           '本轮周期上下文缺失：给了对比基准 base_period_id，但未给出经营'
                           '目标期 target_period_id（基准必须挂在某一期上，不从题面猜）', meta)
        no_period_context = True

    sel = str(product_scope or '').strip()
    ids, scope_kind = [], ''
    if sel == all_label:
        ids, scope_kind = list(all_ids), 'catalog'
    elif sel in cat_ids:
        ids, scope_kind = list(cat_ids[sel]), 'category'
    elif sel in by_label:
        ids, scope_kind = [by_label[sel]], 'single_product'
    else:
        return _reject('product_scope_invalid',
                       'product 选择不在下发目录选项内（选择器带入身份；不从题面文字反推对象）', meta)

    period_by_id = {str(p.get('period_id')): p for p in periods}
    # 目标期和比较基期决定回答口径；可查询范围仍为下发的完整周期目录。
    chronological = sorted(periods, key=_period_time_key)
    period_ids = [str(p.get('period_id')) for p in chronological]
    period_pair = None
    base_p = None
    base_source = ''

    if not no_period_context:
        target_p = period_by_id.get(target_sel)
        if target_p is None:
            return _reject('target_period_unknown',
                           '目标周期 %s 不在下发周期目录内：界面只能选数据实际支持的周期，'
                           '不接受目录外的任意日期' % target_sel, meta)
        if base_sel:
            base_p = period_by_id.get(base_sel)
            if base_p is None:
                return _reject('period_pair_unknown',
                               '所选周期不在下发周期目录内：%s（界面只能选数据实际支持的周期，'
                               '不接受目录外的任意日期）' % base_sel, meta)
            if base_sel == target_sel:
                return _reject('period_pair_identical',
                               '基准周期与目标周期不能相同（%s）' % base_sel, meta)
            if _period_time_key(base_p) >= _period_time_key(target_p):
                return _reject('period_pair_order',
                               '基准周期必须早于目标周期：基准=%s（%s~%s）不早于目标=%s（%s~%s）；'
                               '先后以目录日期区间为准，不按周期 ID 字符串判断'
                               % (base_sel, base_p.get('period_start_date'),
                                  base_p.get('period_end_date'), target_sel,
                                  target_p.get('period_start_date'),
                                  target_p.get('period_end_date')), meta)
            base_source = 'user_selected'
        else:
            pos = [str(p.get('period_id')) for p in chronological].index(target_sel)
            if pos > 0:
                base_p = chronological[pos - 1]
                base_source = 'auto_previous'
            else:
                base_source = 'none'
        if base_p is not None:
            period_pair = {'base_period_id': str(base_p.get('period_id')),
                           'target_period_id': target_sel, 'source': base_source}

    # 显式选择的品类限定调查对象；选择全池时仍可根据题面缩小范围。
    scope_note = ('；用户显式选定的品类，本轮调查以该品类成员为上界，不擅自扩大到全池'
                  if scope_kind == 'category' else '')

    selection = {'product_scope_label': sel, 'scope_kind': scope_kind,
                 'selected_product_ids': ids,
                 'period_ids': period_ids, 'period_pair': period_pair,
                 'target_period_id': target_sel,
                 'base_period_id': str((base_p or {}).get('period_id') or ''),
                 'base_source': base_source,
                 'benchmark_pool': list(all_ids),
                 'binding_evidence': '标签→product_id 映射由服务端取上下文下发的目录完成'
                                     '（运行时取数，非构建期副本、非模型转述）'}
    out_meta = {'entry_contract_version': ENTRY_CONTRACT_VERSION, 'status': 'ok',
                'selection': selection,
                'shared_business_note_version': SHARED_NOTE_VERSION,
                'data_versions': versions,
                'note': '对象/范围/周期由原生选择带入；目录/周期/口径由取上下文端点运行时下发'
                        '；主 Agent 自行决定查询/补查/结束；'
                        '工具=oagent_query（当前数据与对象事实）与 knowledge_search（经营方法）；'
                        '数据为 Demo 模拟口径'}
    # 目录价随商品目录注入任务输入，与成交额指标使用同一模拟价格口径。
    catalog_lines = '\n'.join(
        '- %s ｜ %s（%s）'
        % (p.get('product_id'), p.get('product_name'),
           '，'.join((['品牌 %s' % p['brand']] if p.get('brand') else [])
                     + ['类目 %s' % p.get('category'), '城市 %s' % p.get('city')]
                     + (['价格 %s 元' % p['price']] if p.get('price') is not None else [])))
        for p in products)
    period_lines = '；'.join('%s=%s %s~%s（%s 天）'
                            % (p.get('period_id'), p.get('period_label'),
                               p.get('period_start_date'), p.get('period_end_date'),
                               p.get('length_days')) for p in periods)
    latest_id = str(chronological[-1].get('period_id'))
    if no_period_context:
        target_line = ('（未指定经营目标期：按题面里的日期/窗口理解；题面未指定时按 T02 '
                       '模拟业务日期（2026-05-27 为“昨天”）解释）')
        base_line = '（未指定比较基准：题面点名比较期时按题面选目录内合法周期，并写清两期身份）'
        usage_line = (
            '没有指定经营目标期时，不要自行挑一期当结论落点：只回答题面真正问到的那一期'
            '（单期水平用 period_id 的单期 get_metric_values/compare_product_set），'
            '需要跨期比较时用 compare_periods/filter_products 显式给 base_period_id、'
            'target_period_id，不用缺省窗口；无论按哪种口径比较，都在回答里写清实际用的是哪两期。'
            '相对时间 time 语义的「latest」锚在目录最新期 %s。' % latest_id)
        time_priority_line = ('题面显式指定日期、月份或日期范围时，以题面的时间要求为准；'
                               '题面没有指定时按上面的模拟业务日期解释。')
    else:
        target_line = '%s（%s %s~%s）——本轮经营结论落在这一期' % (
            target_sel, target_p.get('period_label'), target_p.get('period_start_date'),
            target_p.get('period_end_date'))
        if base_p is not None:
            base_id = str(base_p.get('period_id'))
            base_line = '%s（%s %s~%s；%s）' % (
                base_p.get('period_id'), base_p.get('period_label'),
                base_p.get('period_start_date'), base_p.get('period_end_date'),
                '用户在入口显式指定的对比基准' if base_source == 'user_selected'
                else '入口按目录时间序给的默认参考期＝目标期的相邻上一可比期')
            # 自动档只是缺省参考，不替 Agent 定比较方案：题面点名的合法比较期优先。
            if base_source == 'user_selected':
                compare_line = (
                    '跨期比较默认就用上面这两期（用户在入口显式选定的基准与目标期）：'
                    'compare_periods/filter_products 显式给 base_period_id=%s、'
                    'target_period_id=%s，不用缺省窗口。' % (base_id, target_sel))
            else:
                compare_line = (
                    '上面的基准只是缺省参考期，不是必须采用的比较期：题面没点名比较期时，'
                    '问「下滑/上升/比以前」就与它比（compare_periods/filter_products 显式给 '
                    'base_period_id=%s、target_period_id=%s，不用缺省窗口）；题面点名了目录内'
                    '其它合法周期时按题面选实际比较窗口，不套用这期默认基准。' % (base_id, target_sel))
            usage_line = (
                compare_line +
                '只问这一期的水平时用 period_id=%s 的单期 get_metric_values/compare_product_set，'
                '不为形式强行跨期。'
                '无论按哪种口径比较，都在回答里写清实际用的是哪两期。相对时间 time 语义的'
                '「latest」锚在目录最新期 %s；本轮目标期不是最新期时直接用显式周期 ID，不要让 '
                'latest/previous 顶替目标期。' % (target_sel, latest_id))
        else:
            base_line = '（无：目标期是目录内最早一期，目录里没有更早的可比周期）'
            usage_line = ('目录内没有早于目标期的缺省基准：只问本期水平时给 period_id=%s 的单期读数，'
                          '不宣称跨期涨跌；题面点名了目录内其它合法周期时，按题面确定实际比较窗口'
                          '并写清两期身份。' % target_sel)
        time_priority_line = ('题面显式指定日期、月份或日期范围时，以题面的时间要求为准（经营目标期'
                              '与上面的默认比较基准此时只作参考）；题面没有指定时才用经营目标期及默认比较基准。')
    agent_input = (
        '【任务输入】\n'
        '- 分析对象：%s（%s%s）\n'
        '- 经营目标期：%s\n'
        '- 比较基准：%s\n'
        '- 周期用法：%s\n'
        '- 允许周期（周期形态的数据访问范围）：%s\n'
        '- 可查询范围：下方商品目录全部商品（benchmark 池）；日期形态查询（昨天/前天/'
        '上周同一天/最近几天/完整月/月内同进度/显式日期区间）按日级数据覆盖 %s~%s 解析，'
        '不因周期选择器收窄\n'
        '- 时间优先级：%s\n'
        '【商品目录（服务端运行时下发；查询只用目录内 product_id，禁止虚构）】\n%s\n'
        '【周期目录（服务端运行时下发；本轮可查周期=目录全部 %d 期，服务端按目录日期区间'
        '确定性解析；周期形态之外的日期窗口按日级覆盖 %s~%s 解析）】%s\n'
        '%s\n'
        '【问题】\n%s'
        % (sel, scope_kind, scope_note, target_line, base_line, usage_line,
           '/'.join(period_ids), OC.DATA_COVERAGE_START, OC.DATA_COVERAGE_END,
           time_priority_line, catalog_lines, len(period_ids), OC.DATA_COVERAGE_START,
           OC.DATA_COVERAGE_END, period_lines,
           str(parsed.get('shared_business_note') or ''), q))
    root_init = {'run_id': rid, 'allowed_periods': period_ids, 'expected_versions': versions}
    # 仅显式选择品类时限制商品范围；单品调查仍可使用全池作比较基准。
    if scope_kind == 'category':
        root_init['allowed_product_ids'] = list(ids)
    status_body = {'run_id': rid}
    return {'status': 'ok', 'reject_code': '', 'reject_detail': '', 'rejection_json': None,
            'run_id': rid, 'user_question': q,
            'ctx_json': json.dumps(parsed, ensure_ascii=False),
            'selected_product_ids_json': json.dumps(ids, ensure_ascii=False),
            'benchmark_product_ids_json': json.dumps(all_ids, ensure_ascii=False),
            'period_ids_json': json.dumps(period_ids, ensure_ascii=False),
            'selection_json': json.dumps(selection, ensure_ascii=False),
            'entry_meta_json': json.dumps(out_meta, ensure_ascii=False),
            'agent_input_json': agent_input,
            'root_init_body': json.dumps(root_init, ensure_ascii=False),
            'status_body': json.dumps(status_body, ensure_ascii=False)}


def assemble_run_input(run_id: str, inputs: dict, ctx: dict) -> dict:
    """将页面提交与入口上下文装配为根任务输入。"""
    fetch_body = json.dumps(ctx, ensure_ascii=False)
    out = main(
        user_question=str(inputs.get('user_question') or ''),
        product_scope=str(inputs.get('product_scope') or ''),
        run_id=str(run_id or ''),
        fetch_body=fetch_body,
        base_period_id=str(inputs.get('base_period_id') or ''),
        target_period_id=str(inputs.get('target_period_id') or ''))
    if out.get('status') != 'ok':
        return {'ok': False, 'reject_code': str(out.get('reject_code') or 'unknown'),
                'reject_detail': str(out.get('reject_detail') or ''), 'run_input': None}
    root_init = json.loads(out['root_init_body'])
    run_input = {
        'run_id': str(root_init.get('run_id') or run_id),
        'user_question': str(inputs.get('user_question') or ''),
        'agent_input': out['agent_input_json'],
        'allowed_periods': list(root_init.get('allowed_periods') or []),
        'allowed_product_ids': list(root_init.get('allowed_product_ids') or []),
        'expected_versions': dict(root_init.get('expected_versions') or {}),
        'selection': json.loads(out['selection_json']),
        'entry_meta': json.loads(out['entry_meta_json']),
    }
    return {'ok': True, 'reject_code': '', 'reject_detail': '', 'run_input': run_input}
