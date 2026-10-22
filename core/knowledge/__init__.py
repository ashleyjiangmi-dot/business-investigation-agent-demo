# -*- coding: utf-8 -*-
"""本地经营知识检索包。

知识源为 docs/*.md；build_index 将 Markdown 与元数据切块、嵌入并写入本地 Chroma。
retriever 提供节级检索与 Top2 文档全文交付。索引为派生产物，不入 Git；
修改知识源后使用 python -m core.knowledge.build_index --rebuild 重建。"""
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent
DOCS_DIR = KNOWLEDGE_DIR / 'docs'
CHROMA_DIR = KNOWLEDGE_DIR / '.chroma'

COLLECTION_NAME = 'oagent_knowledge_v1'
DOC_TYPES = ('guide', 'case')
TOP_K_DEFAULT = 3
TOP_K_MAX = 3

DOCUMENT_TOP_N = 2

# 文档治理字段由构建、检索与工具投影共用；status 在构建期校验。
GOVERNANCE_FIELDS = ('status', 'version', 'scope', 'supersedes', 'superseded_by')
STATUS_VALUES = ('active', 'retired')

EMBEDDING_MODEL = 'BAAI/bge-small-zh-v1.5'
QUERY_INSTRUCTION = '为这个句子生成表示以用于检索相关文章：'
