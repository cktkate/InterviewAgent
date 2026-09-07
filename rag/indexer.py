"""
文档加载、分块、向量化入库：
- 支持 Markdown / JSON / TXT 题库
- 使用 SentenceSplitter 分块
- 向量化到 Weaviate，同时构建 BM25 倒排索引
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List
import uuid

from llama_index.core import Document, SimpleDirectoryReader
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.dashscope import DashScopeEmbedding
from loguru import logger

from config import settings
from rag.retrievers.vector_retriever import get_weaviate_client
from rag.metadata_filter import normalize_chunk_metadata


def deterministic_chunk_id(source: str, chunk_index: int, text: str) -> str:
    """Return the same logical ID for both sparse and vector indexes."""
    normalized_text = " ".join(text.split())
    payload = f"{source}|{chunk_index}|{normalized_text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def weaviate_uuid_for_chunk(chunk_id: str) -> str:
    """Map a logical chunk ID to a valid deterministic Weaviate UUID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"interview-agent:{chunk_id}"))


def load_documents(kb_dir: str = "rag/knowledge_base") -> List[Document]:
    """递归加载知识库目录下的所有文档"""
    kb_path = Path(kb_dir)
    if not kb_path.exists():
        logger.warning("知识库目录不存在：{}", kb_path)
        return []
    reader = SimpleDirectoryReader(
        input_dir=str(kb_path),
        recursive=True,
        required_exts=[".md", ".txt", ".json", ".pdf"],
    )
    docs = reader.load_data()
    root = kb_path.resolve()
    for doc in docs:
        metadata = doc.metadata or {}
        file_path = metadata.get("file_path")
        if file_path:
            try:
                metadata["source_path"] = Path(file_path).resolve().relative_to(root).as_posix()
            except ValueError:
                metadata["source_path"] = Path(file_path).name
        doc.metadata = metadata
    logger.info("加载文档 {} 篇", len(docs))
    return docs


def split_documents(docs: List[Document], chunk_size: int = 512) -> List[dict]:
    """将文档切分为节点，返回节点列表"""
    splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=64)
    nodes = splitter.get_nodes_from_documents(docs)
    source_positions = {}
    chunks = []
    for node in nodes:
        metadata = dict(node.metadata or {})
        source = str(
            metadata.get("source_path")
            or metadata.get("file_name")
            or metadata.get("file_path")
            or "unknown"
        )
        position = source_positions.get(source, 0)
        source_positions[source] = position + 1
        chunk_id = deterministic_chunk_id(source, position, node.text)
        metadata.update(
            {
                "chunk_id": chunk_id,
                "source_path": source,
                "chunk_index": position,
            }
        )
        metadata = normalize_chunk_metadata(metadata, text=node.text)
        chunks.append({"id": chunk_id, "text": node.text, "metadata": metadata})
    return chunks


def build_vector_index(nodes: List[dict], *, recreate: bool = True) -> None:
    """将节点批量写入 Weaviate；完整重建默认移除旧的重复对象。"""
    embed_model = DashScopeEmbedding(
        model_name=settings.embedding_model,
        api_key=settings.dashscope_api_key,
    )
    client = get_weaviate_client()

    # 创建 / 复用 Collection
    collection_name = "InterviewQuestions"
    if recreate and client.collections.exists(collection_name):
        client.collections.delete(collection_name)
        logger.info("删除旧 Weaviate collection，准备幂等重建: {}", collection_name)
    if not client.collections.exists(collection_name):
        from weaviate.classes.config import DataType, Property

        client.collections.create(
            name=collection_name,
            properties=[
                Property(name="text", data_type=DataType.TEXT),
                Property(name="chunk_id", data_type=DataType.TEXT),
                Property(name="source", data_type=DataType.TEXT),
                Property(name="source_type", data_type=DataType.TEXT),
                Property(name="tech_stack", data_type=DataType.TEXT_ARRAY),
                Property(name="job_domains", data_type=DataType.TEXT_ARRAY),
                Property(name="knowledge_points", data_type=DataType.TEXT_ARRAY),
                Property(name="question_types", data_type=DataType.TEXT_ARRAY),
                Property(name="difficulty", data_type=DataType.TEXT),
                Property(name="visibility", data_type=DataType.TEXT),
                Property(name="owner_id", data_type=DataType.TEXT),
            ],
            vectorizer_config=None,  # 使用外部 embedding
        )
        logger.info("创建 Weaviate collection: {}", collection_name)

    col = client.collections.get(collection_name)
    try:
        with col.batch.dynamic() as batch:
            for n in nodes:
                vec = embed_model.get_text_embedding(n["text"])
                meta = n.get("metadata", {}) or {}
                meta = normalize_chunk_metadata(meta, text=n["text"])
                batch.add_object(
                    properties={
                        "text": n["text"],
                        "chunk_id": n["id"],
                        "source": meta.get("source_path", meta.get("file_path", "")),
                        "source_type": meta.get("source_type", "text"),
                        "tech_stack": meta.get("tech_stack", []),
                        "job_domains": meta.get("job_domains", []),
                        "knowledge_points": meta.get("knowledge_points", []),
                        "question_types": meta.get("question_types", []),
                        "difficulty": meta.get("difficulty", "medium"),
                        "visibility": meta.get("visibility", "public"),
                        "owner_id": meta.get("owner_id", ""),
                    },
                    uuid=weaviate_uuid_for_chunk(n["id"]),
                    vector=vec,
                )
    finally:
        client.close()
    logger.info("向量索引写入完成：{} 条", len(nodes))


def build_full_index(kb_dir: str = "rag/knowledge_base") -> None:
    """构建完整索引：向量 + BM25"""
    docs = load_documents(kb_dir)
    nodes = split_documents(docs)
    build_vector_index(nodes)
    # BM25 索引由 BM25Retriever 运行时基于本地 JSON 构建
    from rag.retrievers.bm25_retriever import BM25Retriever

    BM25Retriever.build_corpus_cache(nodes)
    logger.success("🎉 RAG 索引构建完成")
