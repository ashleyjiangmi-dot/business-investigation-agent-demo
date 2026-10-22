# -*- coding: utf-8 -*-
"""逐视频分发资格规则，供周期查询筛选 /的有效配对行。"""


def video_eligibility(v):
    """逐视频分发资格（与 rule_layer 同口径）：is_public ∧ has_valid_poi ∧
    has_commercial_tag ∧ ¬has_violation_risk。返回 (合格?, 不合格原因列表)。"""
    reasons = []
    if v.get('is_public') not in (True, '是', 'true', 1):
        reasons.append('未公开(is_public=false)')
    if v.get('has_valid_poi') not in (True, '是', 'true', 1):
        reasons.append('无有效POI(has_valid_poi=false)')
    if v.get('has_commercial_tag') not in (True, '是', 'true', 1):
        reasons.append('无商业标签(has_commercial_tag=false)')
    if v.get('has_violation_risk') in (True, '是', 'true', 1):
        reasons.append('存在违规风险(has_violation_risk=true)')
    return (not reasons), reasons
