# -*- coding: utf-8 -*-
"""MCP stdio 查询服务，将工具请求转换为数据服务的只读查询。

工具定义来自 contract，根任务身份由环境变量或启动参数绑定。
通过 ToolServerClient 复用数据层查询、预算与证据登记，不另建业务计算。"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from agents_oagent.mcp_query_tools import contract
from core.tool_client import ToolHttpError, ToolServerClient

SERVER_NAME = 'oagent-query-tools'
SERVER_VERSION = '1.0.0'

RUN_ID_ENV = 'OAGENT_MCP_RUN_ID'
BASE_URL_ENV = 'OAGENT_TOOL_BASE_URL'
DEFAULT_BASE_URL = 'http://127.0.0.1:8099'

INSTRUCTIONS = (
    '经营查询工具：单期指标读数、跨期比较、集合比较。本会话已绑定一个已初始化的根任务，'
    '查询预算、缓存与证据引用由数据服务统一登记；工具参数里不需要也不接受 run_id。'
)


class RunBindingError(RuntimeError):
    """启动配置缺运行身份（不是业务拒绝，直接拒绝启动）。"""


def _tools_from(specs: dict):
    """合同快照 → MCP `Tool` 列表（一次 `tool_specs()` 的投影，不跨快照混用）。"""
    return [types.Tool(name=name, description=spec['description'],
                       input_schema=spec['inputSchema'])
            for name, spec in specs.items()]


def tool_definitions():
    """`tools/list` 的唯一取处＝`contract.tool_specs()`；本模块不存第二份 Schema。"""
    return _tools_from(contract.tool_specs())


def _ok(result: dict) -> types.CallToolResult:
    """数据层响应原样返回：文本块＝同一份 JSON，结构化字段＝同一对象（不复制语义）。"""
    return types.CallToolResult(
        content=[types.TextContent(type='text', text=json.dumps(result, ensure_ascii=False))],
        structured_content=result)


def _error(message: str) -> types.CallToolResult:
    """适配层/传输层故障：`is_error` 结果，不产 `period_rejected` 形状的假业务拒绝。"""
    return types.CallToolResult(content=[types.TextContent(type='text', text=message)],
                                is_error=True)


def build_server(run_id: str, client: ToolServerClient, *,
                 name: str = SERVER_NAME, version: str = SERVER_VERSION) -> Server:
    """装配 MCP Server：运行身份在装配时固定，工具面由合同生成。

    `run_id` 来自可信启动配置或宿主 Runtime 绑定，**不来自工具参数**；
    空值直接拒绝装配，避免出现「无身份 Server」被模型侧误用。
    """
    run_id = str(run_id or '').strip()
    if not run_id:
        raise RunBindingError('MCP Server 必须绑定运行身份：run_id 为空（见 %s）' % RUN_ID_ENV)

    specs = contract.tool_specs()
    tools = _tools_from(specs)
    required = {tool: list(spec['inputSchema'].get('required') or [])
                for tool, spec in specs.items()}
    # 单 run 状态在数据层无锁（`core/data` 只锁调用日志）：本 Server 只绑一个根任务，
    # 并发 `call_tool` 必须在本层串起来，否则两次查询可能抢同一个 query_id / 预算号。
    # 这把锁不阻塞事件循环（查询体在线程里跑），只保证「同一根任务的写序列」。
    query_lock = asyncio.Lock()

    async def on_list_tools(_ctx, _params):
        return types.ListToolsResult(tools=copy.deepcopy(tools))

    async def on_call_tool(_ctx, params):
        tool = str(params.name or '')
        if tool not in contract.TOOL_OPS:
            raise MCPError(code=types.INVALID_PARAMS,
                           message='未知工具：%s（本 Server 只提供 %s）'
                                   % (tool, ' / '.join(contract.TOOL_OPS)))
        args = dict(params.arguments or {})
        missing = [key for key in required[tool] if key not in args]
        if missing:
            # 必填键缺失＝request 构造不出来，属适配层拒绝；不补默认值（ 三选一必给）。
            return _error('参数不完整：%s 缺少必填键 %s（见 tools/list 的 inputSchema.required）；'
                          '未向数据服务发出查询。' % (tool, '、'.join(missing)))
        type_errors = contract.check_arg_types(tool, args)
        if type_errors:
            # 类型不对同样构造不出 request：`list('abc')` 会把字符串拆成字符列表，
            # 让非法输入变成另一种请求，必须在进 to_request 之前拦下（MCP Client 不代校验）。
            return _error('参数类型不合法：%s（见 tools/list 的 inputSchema）；'
                          '未向数据服务发出查询。' % '；'.join(type_errors))
        request = contract.to_request(tool, args)   # 固定 op 映射；多余键（含 run_id）不进 request
        try:
            # 同步 httpx 客户端放到工作线程：事件循环不被阻塞（list_tools/ping 仍可应答）；
            # 同一根任务的查询体由 query_lock 串行，避免并发抢号。
            async with query_lock:
                result = await asyncio.to_thread(client.query, run_id, request)
        except ToolHttpError as exc:
            return _error('数据服务调用失败（工具故障，不是业务拒绝）：%s' % exc)
        return _ok(result)

    return Server(name, version=version, instructions=INSTRUCTIONS,
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def serve_stdio(run_id: str, client: ToolServerClient) -> None:
    """stdio 传输：stdin/stdout 即 MCP 线（本函数不向 stdout 写任何非协议内容）。"""
    server = build_server(run_id, client)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv=None) -> int:
    """启动入口：`python -m agents_oagent.mcp_query_tools.server`。

    环境变量：`OAGENT_MCP_RUN_ID`（必填，已 root_init 的根任务 ID）、
    `OAGENT_TOOL_BASE_URL`（可选，默认 `http://127.0.0.1:8099`）、
    `DIAG_TOOLS_API_KEY`（由 `core/tool_client` 读取，本模块不触碰）。
    """
    run_id = os.environ.get(RUN_ID_ENV, '').strip()
    if not run_id:
        sys.stderr.write('MCP Server 启动失败：环境变量 %s 未设置。'
                         '本 Server 必须绑定一个已 root_init 的根任务（运行身份不来自工具参数）。\n'
                         % RUN_ID_ENV)
        return 2
    base_url = (os.environ.get(BASE_URL_ENV) or '').strip() or DEFAULT_BASE_URL
    asyncio.run(serve_stdio(run_id, ToolServerClient(base_url=base_url)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
