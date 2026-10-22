# -*- coding: utf-8 -*-
"""实时服务装配入口：SDK runtime、工具客户端与本地知识检索器。

凭据由 core.config 装载，端口由 AGENTS_OAGENT_SERVING_PORT 配置，启动说明见根 README。"""
from __future__ import annotations

import logging
import os

import uvicorn

from core.config import load_env
from core.knowledge.retriever import KnowledgeRetriever
from core.tool_client import ToolServerClient
from core.usage_ledger import UsageLedger

from . import model_provider
from .server import USAGE_LEDGER_PATH, create_app

log = logging.getLogger('agents_oagent.serve_live')


def build_live_app():
    load_env()                      # OAGENT_RELAY_BASE_URL / OAGENT_RELAY_API_KEY 入 environ
    client = ToolServerClient()     # 8099 真服务（无 transport 注入即 HTTP，Bearer 取用户级 env）
    retriever = KnowledgeRetriever()   # 启动时创建一次（索引缺失=显式失败并提示重建，不每请求重载 BGE）
    log.info('Agents SDK 真机装配：model=%s temperature=%s max_tokens=%s；知识集合=%s',
             model_provider.MODEL_ID, model_provider.model_settings().temperature,
             model_provider.model_settings().max_tokens, retriever.collection_name)
    return create_app(model=model_provider.build_model(), client=client,
                      ctx_fetcher=client.ctx_fetch, knowledge_retriever=retriever,
                      ledger=UsageLedger(path=USAGE_LEDGER_PATH))


def main() -> None:
    port = int(os.environ.get('AGENTS_OAGENT_SERVING_PORT') or '8327')
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    log.info('Agents SDK oagent 服务层（真机装配）启动：127.0.0.1:%d', port)
    uvicorn.run(build_live_app(), host='127.0.0.1', port=port, log_level='warning')


if __name__ == '__main__':
    main()
