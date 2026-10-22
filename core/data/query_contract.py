# -*- coding: utf-8 -*-
"""查询拒绝异常与条件校验函数的共享定义。"""


class QueryRejected(Exception):
    """结构化拒绝（合同 错误请求）：确定性拒绝，不猜测、不改写、不进入执行、不执行部分查询。"""

    def __init__(self, code, detail):
        super().__init__('%s: %s' % (code, detail))
        self.code = code
        self.detail = detail


def require(cond, code, detail):
    if not cond:
        raise QueryRejected(code, detail)
