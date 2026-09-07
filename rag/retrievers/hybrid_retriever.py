"""
混合检索器：向量 + BM25 双路并行，RRF 融合去重
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
import hashlib
import time
from typing import Any, Dict, List

from loguru import logger

from config import settings
from rag.metadata_filter import RetrievalMetadataFilter, apply_progressive_filter
from rag.retrievers.bm25_retriever import BM25Retriever
from rag.retrievers.vector_retriever import VectorRetriever


def reciprocal_rank_fusion(
    vector_results: List[Dict[str, Any]],
    bm25_results: List[Dict[str, Any]],
    *,
    vector_weight: float = 0.6,
    bm25_weight: float = 0.4,
    k: int = 60,
) -> List[Dict[str, Any]]:
    """RRF 融合：score = w1/(k+rank1) + w2/(k+rank2)"""
    score_map: Dict[str, Dict[str, Any]] = {}

    def fusion_key(result: Dict[str, Any]) -> str:
        """Match the same passage even when two indexes use different IDs."""
        text = " ".join(str(result.get("text") or "").split()).casefold()
        metadata = dict(result.get("metadata") or {})
        source = next((
            str(metadata.get(key) or "").replace("\\", "/").casefold().rsplit("/", 1)[-1]
            for key in ("source_path", "file_path", "file_name", "source")
            if metadata.get(key)
        ), "")
        if text:
            fingerprint = hashlib.sha256(
                f"{source}\0{text}".encode("utf-8")
            ).hexdigest()
            return f"content:{fingerprint}"
        return f"id:{result['id']}"

    def add_branch(
        results: List[Dict[str, Any]], branch: str, weight: float
    ) -> None:
        seen = set()
        for rank, result in enumerate(results, start=1):
            rid = str(result["id"])
            key = fusion_key(result)
            if key in seen:
                continue
            seen.add(key)
            entry = score_map.setdefault(
                key,
                {"doc": result, "rrf": 0.0, "retrieval": {}},
            )
            entry["rrf"] += weight / (k + rank)
            entry["retrieval"].update(
                {
                    f"{branch}_rank": rank,
                    f"{branch}_score": float(result.get("score", 0.0)),
                    f"{branch}_id": rid,
                }
            )

    add_branch(vector_results, "vector", vector_weight)
    add_branch(bm25_results, "bm25", bm25_weight)

    merged = sorted(score_map.values(), key=lambda x: x["rrf"], reverse=True)
    output = []
    for fusion_rank, item in enumerate(merged, start=1):
        retrieval = dict(item["retrieval"])
        retrieval.update(
            {
                "rrf_rank": fusion_rank,
                "rrf_score": item["rrf"],
                "matched_branches": sorted(
                    key.removesuffix("_rank")
                    for key in retrieval
                    if key.endswith("_rank") and key != "rrf_rank"
                ),
            }
        )
        branches = retrieval["matched_branches"]
        output.append(
            {
                "id": item["doc"]["id"],
                "text": item["doc"]["text"],
                "metadata": item["doc"].get("metadata", {}),
                "score": item["rrf"],
                "source": "hybrid" if len(branches) > 1 else branches[0],
                "retrieval": retrieval,
            }
        )
    return output


class HybridRetriever:
    """向量 + BM25 双路混合检索"""

    def __init__(
        self,
        vector: Any = None,
        bm25: Any = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.last_diagnostics: Dict[str, Any] = {}
        self.timeout_seconds = timeout_seconds or settings.rag_retrieval_timeout_seconds
        if vector is not None:
            self.vector = vector
        else:
            try:
                self.vector = VectorRetriever()
            except Exception as exc:
                self.vector = None
                self.last_diagnostics["vector_init"] = {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
                logger.warning("Vector retriever 初始化失败，将使用 BM25 降级: {}", exc)
        if bm25 is not None:
            self.bm25 = bm25
        else:
            try:
                self.bm25 = BM25Retriever()
            except Exception as exc:
                self.bm25 = None
                self.last_diagnostics["bm25_init"] = {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
                logger.warning("BM25 retriever 初始化失败，将使用向量降级: {}", exc)

    @staticmethod
    def _retrieve_branch(
        name: str,
        retriever: Any,
        query: str,
        top_k: int,
        metadata_filter: RetrievalMetadataFilter | None = None,
    ):
        started = time.perf_counter()
        try:
            if metadata_filter is None:
                results = retriever.retrieve(query, top_k=top_k)
            else:
                try:
                    results = retriever.retrieve(
                        query, top_k=top_k, metadata_filter=metadata_filter
                    )
                except TypeError as exc:
                    if "metadata_filter" not in str(exc):
                        raise
                    results = retriever.retrieve(query, top_k=top_k)
            return name, results, {
                "status": "success",
                "count": len(results),
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        except Exception as exc:
            return name, [], {
                "status": "failed",
                "count": 0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "error_type": type(exc).__name__,
            }

    def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        *,
        lexical_query: str | None = None,
        metadata_filter: RetrievalMetadataFilter | None = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve with the original semantic query and optional BM25 expansion."""
        top_k = top_k or settings.rag_top_k
        branch_results = {"vector": [], "bm25": []}
        diagnostics = dict(self.last_diagnostics)
        branches = {
            name: retriever
            for name, retriever in (("vector", self.vector), ("bm25", self.bm25))
            if retriever is not None
        }
        if not branches:
            self.last_diagnostics = diagnostics
            return []

        executor = ThreadPoolExecutor(max_workers=len(branches), thread_name_prefix="rag")
        futures = {
            executor.submit(
                self._retrieve_branch,
                name,
                retriever,
                lexical_query if name == "bm25" and lexical_query else query,
                top_k * 2,
                metadata_filter,
            ): name
            for name, retriever in branches.items()
        }
        done, pending = wait(futures, timeout=max(0.1, self.timeout_seconds))
        for future in done:
            name, results, branch_diagnostics = future.result()
            branch_results[name] = results
            diagnostics[name] = branch_diagnostics
        for future in pending:
            name = futures[future]
            future.cancel()
            diagnostics[name] = {
                "status": "timeout",
                "count": 0,
                "timeout_seconds": self.timeout_seconds,
            }
        executor.shutdown(wait=False, cancel_futures=True)

        vec_results = branch_results["vector"]
        bm25_results = branch_results["bm25"]
        logger.debug("Hybrid: vec={} bm25={}", len(vec_results), len(bm25_results))
        merged = reciprocal_rank_fusion(
            vec_results,
            bm25_results,
            vector_weight=settings.rag_vector_weight,
            bm25_weight=settings.rag_bm25_weight,
        )
        diagnostics["fusion"] = {
            "status": "success" if merged else "empty",
            "count": len(merged),
        }
        selected, filter_diagnostics = apply_progressive_filter(
            merged, metadata_filter, top_k=top_k
        )
        diagnostics["metadata_filter"] = filter_diagnostics
        self.last_diagnostics = diagnostics
        return selected
