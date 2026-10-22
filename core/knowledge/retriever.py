# -*- coding: utf-8 -*-
"""本地经营知识检索接口。

search 提供节级检索：query 不得为空，doc_type 限 guide/case，top_k 上限为 3。
search_documents 按候选聚合选择 Top2 文档，从 Markdown 知识源交付全文。
交付模式由 root_contract.KNOWLEDGE_DELIVERY_MODE 选择。
不重写 query，不将相似度作为置信度；索引缺失或损坏时明确报错。"""
from __future__ import annotations

from core.knowledge import (CHROMA_DIR, COLLECTION_NAME, DOC_TYPES,
                                        DOCUMENT_TOP_N, GOVERNANCE_FIELDS, TOP_K_DEFAULT,
                                        TOP_K_MAX)
from core.knowledge.build_index import BGEEmbedder, canonical_documents


class KnowledgeRetriever:
    """本地知识检索器：加载已建索引，提供 search 统一接口。"""

    def __init__(self, persist_dir=CHROMA_DIR, collection_name=COLLECTION_NAME,
                 embedder=None):
        import chromadb
        from chromadb.config import Settings

        self._persist_dir = str(persist_dir)
        self.collection_name = str(collection_name)   # 公开只读（装配日志/诊断用）
        # embedder 可注入，便于离线验证完整索引和检索路径。
        self._embedder = embedder if embedder is not None else BGEEmbedder()
        self._client = chromadb.PersistentClient(
            path=self._persist_dir, settings=Settings(anonymized_telemetry=False))
        try:
            self._collection = self._client.get_collection(collection_name)
        except Exception as exc:
            raise RuntimeError(
                f'知识索引 {collection_name!r} 不存在或不可读（persist={self._persist_dir}）；'
                '请先运行 python -m core.knowledge.build_index --rebuild') from exc
        self._canonical: dict[str, str] | None = None   # lazy：canonical 全文按需加载一次

    @property
    def index_fingerprint(self) -> dict:
        """读取索引构建时登记的语料指纹与块数；缺失值返回 None，由调用方判断是否需要重建。"""
        meta = self._collection.metadata or {}
        return {'corpus_sha256': meta.get('corpus_sha256'),
                'chunk_count': meta.get('chunk_count'),
                'stored_chunks': self._collection.count()}

    def search(self, query: str, doc_type: str | None = None,
               top_k: int = TOP_K_DEFAULT) -> dict:
        """按语义检索知识 chunk；输入校验不通过抛 ValueError，底层异常原样明确抛出。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError('query 不能为空')
        if doc_type not in (None,) + DOC_TYPES:
            raise ValueError(
                f"doc_type={doc_type!r} 非法（只允许 None|'guide'|'case'）")
        try:
            top_k = int(top_k)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'top_k={top_k!r} 非法（须为整数）') from exc
        if top_k < 1:
            raise ValueError(f'top_k={top_k} 非法（至少 1）')
        top_k = min(top_k, TOP_K_MAX)

        where = {'doc_type': doc_type} if doc_type else None
        res = self._collection.query(
            query_embeddings=[self._embedder.embed_query(query.strip())],
            n_results=top_k,
            where=where,
            include=['documents', 'metadatas'])
        results = []
        metadatas = res['metadatas'][0] or []
        documents = res['documents'][0] or []
        for meta, content in zip(metadatas, documents):
            results.append({
                'chunk_id': meta.get('chunk_id'),
                'doc_id': meta.get('doc_id'),
                'title': meta.get('title'),
                'section': meta.get('section'),
                'doc_type': meta.get('doc_type'),
                'content': content,
                # 治理字段：随结果原样回传，索引里没写=None（缺失不是"现行"）；
                # 不参与打分、不做 filter、不进 embedding 文本。
                **{f: meta.get(f) for f in GOVERNANCE_FIELDS},
            })
        return {'ok': True, 'query': query, 'results': results}

    def _canonical_full_documents(self) -> dict[str, str]:
        """canonical 全文单点加载（懒加载一次）；索引 id 与语料 doc_id 失配时显式报错。"""
        if self._canonical is None:
            self._canonical = canonical_documents()
        return self._canonical

    def search_documents(self, query: str, doc_type: str | None = None) -> dict:
        """在完整候选面按文档的最高 chunk 相似度排序，交付固定 Top2 篇全文。

与节级检索共用 query、embedder、collection 与 doc_type 过滤。
正文来自 canonical_documents，治理元数据取最佳 chunk；不裁剪或拼接子块。
非法参数、索引与知识源失配均明确报错。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError('query 不能为空')
        if doc_type not in (None,) + DOC_TYPES:
            raise ValueError(
                f"doc_type={doc_type!r} 非法（只允许 None|'guide'|'case'）")

        where = {'doc_type': doc_type} if doc_type else None
        total = self._collection.count()
        if total == 0:
            return {'ok': True, 'query': query, 'doc_type': doc_type,
                    'delivery_mode': 'document', 'delivery_source': 'canonical_markdown',
                    'pool_chunks': 0, 'documents': [], 'document_rank': []}
        res = self._collection.query(
            query_embeddings=[self._embedder.embed_query(query.strip())],
            n_results=total,
            where=where,
            include=['metadatas', 'distances'])
        hits = list(zip(res['metadatas'][0] or [], res['distances'][0] or []))

        # 文档得分取最佳块的余弦相似度，并保留该块元数据；同分文档按候选出现顺序排序。
        best: dict[str, dict] = {}
        first_pos: dict[str, int] = {}
        member_counts: dict[str, int] = {}
        for i, (meta, dist) in enumerate(hits):
            doc_id = meta.get('doc_id')
            sim = 1.0 - float(dist)
            member_counts[doc_id] = member_counts.get(doc_id, 0) + 1
            first_pos.setdefault(doc_id, i)
            if doc_id not in best or sim > best[doc_id]['doc_score']:
                best[doc_id] = {'doc_id': doc_id, 'title': meta.get('title'),
                                'doc_type': meta.get('doc_type'), 'doc_score': sim,
                                'best_chunk_id': meta.get('chunk_id'),
                                **{f: meta.get(f) for f in GOVERNANCE_FIELDS}}
        ranked = sorted(best.values(),
                        key=lambda r: (-r['doc_score'], first_pos[r['doc_id']]))
        document_rank = [{'rank': i + 1, 'doc_id': r['doc_id'],
                          'doc_score': r['doc_score'],
                          'best_chunk_id': r['best_chunk_id']}
                         for i, r in enumerate(ranked)]

        canonical = self._canonical_full_documents()
        documents = []
        for i, r in enumerate(ranked[:DOCUMENT_TOP_N]):
            doc_id = r['doc_id']
            if doc_id not in canonical:
                raise RuntimeError(
                    f'document {doc_id!r} 在 canonical source 中不存在'
                    '（索引与语料失配；请确认索引按当前 docs/ 重建）')
            content = canonical[doc_id]
            documents.append({
                'rank': i + 1,
                'doc_id': doc_id,
                'title': r['title'],
                'doc_type': r['doc_type'],
                'doc_score': r['doc_score'],
                'best_chunk_id': r['best_chunk_id'],
                'candidate_chunks': member_counts[doc_id],
                'delivered_chars': len(content),
                'content': content,
                **{f: r[f] for f in GOVERNANCE_FIELDS},
            })
        return {'ok': True, 'query': query, 'doc_type': doc_type,
                'delivery_mode': 'document', 'delivery_source': 'canonical_markdown',
                'pool_chunks': len(hits), 'documents': documents,
                'document_rank': document_rank}
