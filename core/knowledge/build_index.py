# -*- coding: utf-8 -*-
"""构建或重建本地 Chroma 知识索引。

知识源为 docs/*.md，必填 doc_id/doc_type，治理字段按实际存在值透传。
按 H2/H3 切节，超窗节按条目分段；chunk_id 按文档与章节确定，同名追加序号。
嵌入文本含文档与章节上下文；全文与整节从 Markdown 恢复。
默认拒绝覆盖已存在的 collection，显式 --rebuild 后重建并登记语料指纹。"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys

import yaml
from langchain_text_splitters import MarkdownHeaderTextSplitter

from core.knowledge import (CHROMA_DIR, COLLECTION_NAME, DOCS_DIR, DOC_TYPES,
                                        EMBEDDING_MODEL, GOVERNANCE_FIELDS, QUERY_INSTRUCTION,
                                        STATUS_VALUES)

_FRONT_MATTER_RE = re.compile(r'\A---\n(.*?)\n---\n', re.S)
_H1_RE = re.compile(r'^# (.+?)\s*$', re.M)
_REQUIRED_FIELDS = ('doc_id', 'doc_type')


# ---------- 文档加载 ----------

def parse_front_matter(text: str, source_name: str) -> tuple[dict, str]:
    """拆 YAML front matter 与正文；缺围栏/坏 YAML/缺字段都报出具体文件名，不静默跳过。"""
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        raise ValueError(f'{source_name}: 缺少 YAML front matter（文件须以 --- 围栏开头）')
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f'{source_name}: front matter 不是合法 YAML: {exc}') from exc
    if not isinstance(meta, dict):
        raise ValueError(f'{source_name}: front matter 须是键值映射')
    missing = [k for k in _REQUIRED_FIELDS if k not in meta]
    if missing:
        raise ValueError(f'{source_name}: front matter 缺少必要字段 {missing}')
    return meta, text[m.end():]


def governance_of(meta: dict, source_name: str) -> dict:
    """取 front matter 里的治理字段（metadata contract）。

    只透传语料实际写了的键：没写=缺失（不填默认值、不猜 active），值须是非空标量字符串。
    `status` 限枚举（STATUS_VALUES）：写错第三种取值时构建期报错，不静默丢——丢掉的后果是
    下游那条 retired 确定性提示悄悄失效，比构建失败更难发现。
    """
    out = {}
    for field in GOVERNANCE_FIELDS:
        if field not in meta or meta[field] is None:
            continue
        raw = meta[field]
        if isinstance(raw, (list, dict)):
            raise ValueError(f'{source_name}: 治理字段 {field} 须是标量（实际 {type(raw).__name__}）；'
                             'supersedes / superseded_by 仅支持单个文档 ID')
        value = str(raw).strip()
        if not value:
            raise ValueError(f'{source_name}: 治理字段 {field} 为空（要么不写，要么写值）')
        if field == 'status' and value not in STATUS_VALUES:
            raise ValueError(
                f'{source_name}: status={value!r} 非法（只允许 {STATUS_VALUES}）')
        out[field] = value
    return out


def load_docs(docs_dir=DOCS_DIR) -> list[dict]:
    """读 docs/*.md，校验 metadata 合同，返回按文件名稳定排序的文档列表。"""
    from pathlib import Path

    docs_dir = Path(docs_dir)
    files = sorted(docs_dir.glob('*.md'))
    if not files:
        raise ValueError(f'{docs_dir}: 没有找到任何 *.md 知识文档')
    docs = []
    for path in files:
        meta, body = parse_front_matter(path.read_text(encoding='utf-8'), path.name)
        if meta['doc_type'] not in DOC_TYPES:
            raise ValueError(
                f"{path.name}: doc_type={meta['doc_type']!r} 非法（只允许 {DOC_TYPES}）")
        h1 = _H1_RE.search(body)
        docs.append({
            'doc_id': str(meta['doc_id']),
            'doc_type': str(meta['doc_type']),
            'title': h1.group(1).strip() if h1 else str(meta['doc_id']),
            'body': body,
            'source_file': path.name,
            'governance': governance_of(meta, path.name),
        })
    doc_ids = [d['doc_id'] for d in docs]
    if len(set(doc_ids)) != len(doc_ids):
        raise ValueError(f'doc_id 重复: {sorted(doc_ids)}')
    # 取代关系指向的文档必须真实存在：这条引用会被组装进模型可见的 retired 提示，
    # 指向不存在的 doc_id 等于让 Agent 去核对一个没有的依据（确定性行为的正确性前提）。
    known = set(doc_ids)
    for doc in docs:
        for field in ('supersedes', 'superseded_by'):
            ref = doc['governance'].get(field)
            if ref and ref not in known:
                raise ValueError(f"{doc['doc_id']}: {field}={ref!r} 指向不存在的文档")
    return docs


# ---------- 切块 ----------

EMBED_WINDOW_TOKENS = 512
EMBED_TEXT_CHAR_BUDGET = EMBED_WINDOW_TOKENS - 2   # 510 字

# 条目边界：列表项 / 编号项 / 表行 / 标题 / 加粗小标题。不构成边界的行并入上一条目，
# 所以切点只落在结构边界上，不在句子中间切。
_ITEM_START_RE = re.compile(r'^(?:[-*+]\s|\d+[.)\]）]\s*|[#{1,6}\|]|\*\S)')


def split_items(content: str) -> list[str]:
    """把节正文按 Markdown 条目边界切成条目；续行（如「适合：」「前置证据：」）并入前一条目。"""
    items: list[str] = []
    for line in content.split('\n'):
        if not line.strip():
            continue
        if items and not _ITEM_START_RE.match(line.strip()):
            items[-1] = f'{items[-1]}\n{line}'
        else:
            items.append(line)
    return items


def split_oversized(header: str, content: str, budget: int | None = None) -> list[str]:
    """超出模型窗口的节：按条目边界补出逐段，只让分段作 vector candidate。"""
    budget = EMBED_TEXT_CHAR_BUDGET if budget is None else budget
    room = budget - len(header) - 2          # 扣掉头部与 '\n\n' 分隔符后的正文可用字数
    if len(content) <= room:
        return [content]

    def pack(units: list[str]) -> list[str]:
        parts: list[str] = []
        for unit in units:
            if not unit.strip():
                continue
            if len(unit) > room:             # 单条目自己就超预算：退一层按行装箱
                parts.extend(pack(unit.split('\n')) if '\n' in unit else [unit])
                continue
            if parts and len(parts[-1]) + 1 + len(unit) <= room:
                parts[-1] = f'{parts[-1]}\n{unit}'
            else:
                parts.append(unit)
        return parts

    parts = pack(split_items(content))
    return parts if len(parts) > 1 else [content]


def _split_sections(body: str) -> list:
    """Markdown 正文按 H1/H2/H3 语义切节（chunk_doc 与 canonical_sections 共用同一解析）。"""
    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[('#', 'h1'), ('##', 'h2'), ('###', 'h3')],
        strip_headers=True)
    return splitter.split_text(body)


def _section_name(sec_metadata: dict, doc_title: str) -> str:
    """节名取最细级标题（H3 存在取 H3，否则 H2；无标题落到文档题目）。"""
    h3 = (sec_metadata.get('h3') or '').strip()
    h2 = (sec_metadata.get('h2') or '').strip()
    return h3 or h2 or doc_title


def canonical_sections(docs: list[dict] | None = None) -> dict[str, dict[str, str]]:
    """按与切块一致的标题规则，从 Markdown 恢复 {doc_id: {section: 完整正文}}。

超窗节的分段只用于向量召回，整节交付不依赖索引副本；同名章节按文档顺序覆盖。"""
    docs = load_docs() if docs is None else docs
    out: dict[str, dict[str, str]] = {}
    for doc in docs:
        for sec in _split_sections(doc['body']):
            content = sec.page_content.strip()
            if not content:
                continue
            out.setdefault(doc['doc_id'], {})[_section_name(sec.metadata, doc['title'])] = content
    return out


def canonical_documents(docs: list[dict] | None = None) -> dict[str, str]:
    """从 Markdown 知识源返回 {doc_id: 完整正文}，保留标题与原文顺序。

    全文交付不拼接索引块；节级解析可能折叠空白，全文交付必须使用原始 body。"""
    docs = load_docs() if docs is None else docs
    return {doc['doc_id']: doc['body'].strip() for doc in docs}


def chunk_doc(doc: dict) -> list[dict]:
    """按最细级 H2/H3 标题切块，超窗节按条目继续分段。

    chunk_id 由文档 ID 与章节名组成，重名块追加顺序号；每段嵌入文本均包含文档和章节上下文。
    治理元数据按文档透传，完整章节由 canonical_sections 提供。"""
    used_ids: set[str] = set()
    chunks: list[dict] = []
    for sec in _split_sections(doc['body']):
        section = _section_name(sec.metadata, doc['title'])
        content = sec.page_content.strip()
        if not content:
            continue
        header = f"文档：{doc['title']}\n章节：{section}"
        for part in split_oversized(header, content):
            base = f"{doc['doc_id']}#{section}"
            chunk_id = base
            n = 2
            while chunk_id in used_ids:
                chunk_id = f'{base}-{n}'
                n += 1
            used_ids.add(chunk_id)
            chunks.append({
                'chunk_id': chunk_id,
                'doc_id': doc['doc_id'],
                'title': doc['title'],
                'section': section,
                'content': part,
                'doc_type': doc['doc_type'],
                'governance': doc.get('governance') or {},
                'embed_text': f'{header}\n\n{part}',
            })
    if not chunks:
        raise ValueError(f"{doc['doc_id']}: 未切出任何 chunk")
    return chunks


def chunk_all(docs: list[dict]) -> list[dict]:
    return [c for doc in docs for c in chunk_doc(doc)]


# ---------- 语料指纹 ----------

def corpus_sha256(docs_dir=DOCS_DIR) -> str:
    """按文件名排序计算语料组合指纹，内容或文件名变化都会使索引失效。"""
    from pathlib import Path

    files = sorted(Path(docs_dir).glob('*.md'))
    if not files:
        raise ValueError(f'{docs_dir}: 没有找到任何 *.md 知识文档')
    digest = hashlib.sha256()
    for path in files:
        digest.update(f"{path.name}\n"
                      f"{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode('utf-8'))
    return digest.hexdigest()


def corpus_fingerprint(docs_dir=DOCS_DIR) -> dict:
    """当前语料的实算指纹 = 组合 hash + 按现行切块规则重算出的切块数。"""
    return {'corpus_sha256': corpus_sha256(docs_dir),
            'chunk_count': len(chunk_all(load_docs(docs_dir)))}


# ---------- Embedding ----------

class BGEEmbedder:
    """BAAI/bge-small-zh-v1.5 本地 embedding（sentence-transformers 直连）。

    document/query 处理集中在此单点：document 侧不加指令，query 侧加 BGE 官方检索指令
    前缀；向量归一化，配合 collection 的 cosine 空间（该模型官方 retrieval 推荐配置）。
    不接云 Embedding API。模型加载失败（含网络原因）抛出明确错误；网络受限环境可设
    HF_ENDPOINT=https://hf-mirror.com 后重试（首次下载后走本地缓存）。
    """

    def __init__(self, model_name: str = EMBEDDING_MODEL):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                '缺少 sentence-transformers：请先安装 agents_oagent/requirements.txt'
                '（current runtime 依赖单点，含 RAG 依赖）') from exc
        try:
            self._model = SentenceTransformer(model_name)
        except Exception as exc:
            raise RuntimeError(
                f'加载 embedding 模型 {model_name} 失败：{exc}；网络受限环境可设置 '
                'HF_ENDPOINT=https://hf-mirror.com 后重试') from exc
        self.model_name = model_name

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self._model.encode(
            [QUERY_INSTRUCTION + text], normalize_embeddings=True)[0].tolist()


# ---------- Chroma 存储 ----------

def _chroma_client(persist_dir):
    import chromadb
    from chromadb.config import Settings
    return chromadb.PersistentClient(
        path=str(persist_dir), settings=Settings(anonymized_telemetry=False))


def _existing_collection_names(client) -> set[str]:
    names = set()
    for c in client.list_collections():
        names.add(c.name if hasattr(c, 'name') else str(c))
    return names


def build_index(docs_dir=DOCS_DIR, persist_dir=CHROMA_DIR, collection_name=COLLECTION_NAME,
                embedder=None, rebuild: bool = False) -> dict:
    """从 source docs 构建（或重建）本地 Chroma 索引，返回摘要 dict。

    rebuild=False 且 collection 已存在 → RuntimeError（CLI 会转成退出码 2，防误删）；
    rebuild=True → 先删除本 collection 旧内容再全量写入，幂等（重复执行计数不变）。
    """
    client = _chroma_client(persist_dir)
    existing = _existing_collection_names(client)
    if collection_name in existing and not rebuild:
        raise RuntimeError(
            f'collection {collection_name!r} 已存在于 {persist_dir}；重建请显式加 --rebuild'
            '（会先清空本 collection 旧内容）')
    if embedder is None:
        embedder = BGEEmbedder()
    docs = load_docs(docs_dir)
    chunks = chunk_all(docs)
    fingerprint = {'corpus_sha256': corpus_sha256(docs_dir), 'chunk_count': len(chunks)}
    if collection_name in existing:
        client.delete_collection(collection_name)
    collection = client.create_collection(collection_name, metadata={
        'hnsw:space': 'cosine', **fingerprint})
    embeddings = embedder.embed_documents([c['embed_text'] for c in chunks])
    batch = 50
    for i in range(0, len(chunks), batch):
        part = chunks[i:i + batch]
        collection.add(
            ids=[c['chunk_id'] for c in part],
            embeddings=embeddings[i:i + batch],
            documents=[c['content'] for c in part],
            metadatas=[{
                'chunk_id': c['chunk_id'],
                'doc_id': c['doc_id'],
                'title': c['title'],
                'section': c['section'],
                'doc_type': c['doc_type'],
                **c['governance'],   # 治理字段：语料写了才写键（Chroma metadata 不收 null），缺失不填默认值
            } for c in part])
    return {
        'docs': len(docs),
        'chunks': len(chunks),
        'collection': collection_name,
        'persist': str(persist_dir),
        'embedding_model': getattr(embedder, 'model_name', type(embedder).__name__),
        **fingerprint,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='构建/重建本地知识检索索引')
    parser.add_argument('--rebuild', action='store_true',
                        help='重建前先清空本 collection 旧内容（幂等，不会越建越重复）')
    args = parser.parse_args(argv)
    try:
        summary = build_index(rebuild=args.rebuild)
    except RuntimeError as exc:
        print(f'[build_index] {exc}', file=sys.stderr)
        return 2
    print(f"docs={summary['docs']} chunks={summary['chunks']} "
          f"collection={summary['collection']} persist={summary['persist']} "
          f"corpus_sha256={summary['corpus_sha256'][:8]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
