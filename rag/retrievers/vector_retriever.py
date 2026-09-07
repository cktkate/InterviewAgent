"""Weaviate 向量检索"""
from __future__ import annotations

from typing import Any, Dict, List

import weaviate
from llama_index.embeddings.dashscope import DashScopeEmbedding
from loguru import logger
from weaviate.classes.query import Filter, MetadataQuery

from config import settings
from rag.metadata_filter import (
    RetrievalMetadataFilter,
    apply_security_filter,
    normalize_chunk_metadata,
)


def get_weaviate_client() -> weaviate.WeaviateClient:
    """获取 Weaviate 客户端（单例）"""
    if settings.weaviate_api_key:
        return weaviate.connect_to_weaviate_cloud(
            cluster_url=settings.weaviate_url,
            auth_credentials=weaviate.classes.init.Auth.api_key(settings.weaviate_api_key),
            skip_init_checks=True,
        )
    return weaviate.connect_to_custom(
        http_host=settings.weaviate_url.replace("http://", "").split(":")[0],
        http_port=int(settings.weaviate_url.split(":")[-1]),
        http_secure=settings.weaviate_url.startswith("https"),
        grpc_host=settings.weaviate_url.replace("http://", "").split(":")[0],
        grpc_port=50051,
        grpc_secure=False,
        skip_init_checks=True,
    )


class VectorRetriever:
    """基于 Weaviate 的向量检索"""

    def __init__(self, collection_name: str = "InterviewQuestions"):
        self.client = get_weaviate_client()
        self.collection = self.client.collections.get(collection_name)
        self.embed = DashScopeEmbedding(
            model_name=settings.embedding_model,
            api_key=settings.dashscope_api_key,
        )
        self._filterable_properties: set[str] | None = None
        self.last_filter_diagnostics: Dict[str, Any] = {}

    def _properties(self) -> set[str]:
        if self._filterable_properties is None:
            try:
                config = self.collection.config.get()
                self._filterable_properties = {
                    str(item.name) for item in config.properties
                }
            except Exception:
                self._filterable_properties = set()
        return self._filterable_properties

    def _security_filter(self, metadata_filter: RetrievalMetadataFilter):
        properties = self._properties()
        if "visibility" not in properties:
            return None
        conditions = []
        if metadata_filter.include_public:
            conditions.append(Filter.by_property("visibility").equal("public"))
        if metadata_filter.requester_id and "owner_id" in properties:
            conditions.append(Filter.all_of([
                Filter.by_property("visibility").equal("private"),
                Filter.by_property("owner_id").equal(metadata_filter.requester_id),
            ]))
        if not conditions:
            return Filter.by_property("owner_id").equal("__no_authorized_owner__")
        return conditions[0] if len(conditions) == 1 else Filter.any_of(conditions)

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        *,
        metadata_filter: RetrievalMetadataFilter | None = None,
    ) -> List[Dict[str, Any]]:
        """向量近邻检索"""
        query_vec = self.embed.get_query_embedding(query)
        server_filter = self._security_filter(metadata_filter) if metadata_filter else None
        candidate_limit = top_k * 3 if metadata_filter else top_k
        resp = self.collection.query.near_vector(
            near_vector=query_vec,
            limit=candidate_limit,
            filters=server_filter,
            return_metadata=MetadataQuery(distance=True),
        )
        results: List[Dict[str, Any]] = []
        for obj in resp.objects:
            chunk_id = str(obj.properties.get("chunk_id") or obj.uuid)
            properties = normalize_chunk_metadata(
                obj.properties, text=str(obj.properties.get("text") or "")
            )
            results.append({
                "id": chunk_id,
                "text": obj.properties.get("text", ""),
                "metadata": properties,
                "score": 1.0 - (obj.metadata.distance or 0.0),
                "source": "vector",
                "vector_uuid": str(obj.uuid),
            })
        results = apply_security_filter(results, metadata_filter)[:top_k]
        self.last_filter_diagnostics = {
            "status": "server_and_client" if server_filter is not None else (
                "legacy_client" if metadata_filter else "disabled"
            ),
            "candidate_count": len(resp.objects),
            "result_count": len(results),
        }
        logger.debug("Vector 检索 top_k={} 返回 {} 条", top_k, len(results))
        return results
