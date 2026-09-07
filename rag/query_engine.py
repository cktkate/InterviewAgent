"""
统一查询引擎入口：
- hybrid_query: 混合检索 + RRF 融合
- full_query: 混合检索 + LLM Rerank（精排）
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from config import settings
from rag.metadata_filter import RetrievalMetadataFilter
from rag.retrievers.hybrid_retriever import HybridRetriever
from rag.query_enhancer import enhance_query


def estimate_rerank_confidence(
    candidates: List[Dict[str, Any]],
    diagnostics: Dict[str, Any],
    *,
    top_k: int,
) -> Dict[str, Any]:
    """Estimate whether deterministic hybrid ranking is already stable.

    This is intentionally conservative: degraded single-branch retrieval never
    bypasses reranking. The score uses only operational retrieval evidence, not
    another LLM call.
    """
    window = list(candidates[: max(1, top_k)])
    vector_ok = (diagnostics.get("vector") or {}).get("status") == "success"
    bm25_ok = (diagnostics.get("bm25") or {}).get("status") == "success"
    branch_health = 1.0 if vector_ok and bm25_ok else 0.0

    def retrieval(item: Dict[str, Any]) -> Dict[str, Any]:
        return dict(item.get("retrieval") or {})

    top = retrieval(window[0]) if window else {}
    top_branches = set(top.get("matched_branches") or [])
    top_dual = 1.0 if {"vector", "bm25"} <= top_branches else 0.0
    vector_rank = int(top.get("vector_rank") or 0)
    bm25_rank = int(top.get("bm25_rank") or 0)
    rank_agreement = (
        max(0.0, 1.0 - abs(vector_rank - bm25_rank) / max(1, len(candidates)))
        if vector_rank and bm25_rank
        else 0.0
    )
    dual_branch_ratio = (
        sum(
            1
            for item in window
            if {"vector", "bm25"}
            <= set(retrieval(item).get("matched_branches") or [])
        )
        / len(window)
        if window
        else 0.0
    )
    first_score = float(window[0].get("score") or 0.0) if window else 0.0
    second_score = float(window[1].get("score") or 0.0) if len(window) > 1 else 0.0
    relative_margin = (
        max(0.0, min(1.0, (first_score - second_score) / first_score))
        if first_score > 0
        else 0.0
    )
    enough_results = 1.0 if len(window) >= max(1, top_k) else 0.0
    confidence = (
        0.20 * branch_health
        + 0.30 * top_dual
        + 0.20 * rank_agreement
        + 0.15 * dual_branch_ratio
        + 0.10 * relative_margin
        + 0.05 * enough_results
    )
    return {
        "score": round(confidence, 6),
        "branch_health": branch_health,
        "top_dual_branch": top_dual,
        "top_rank_agreement": round(rank_agreement, 6),
        "dual_branch_ratio": round(dual_branch_ratio, 6),
        "relative_top_margin": round(relative_margin, 6),
        "enough_results": bool(enough_results),
    }


def _mark_rerank_skipped(
    candidates: List[Dict[str, Any]], top_k: int, *, status: str
) -> List[Dict[str, Any]]:
    output = []
    for rank, candidate in enumerate(candidates[:top_k], start=1):
        item = dict(candidate)
        retrieval = dict(item.get("retrieval") or {})
        retrieval.update({
            "rerank_status": status,
            "rerank_rank": rank,
        })
        item["retrieval"] = retrieval
        output.append(item)
    return output


class QueryEngine:
    def __init__(self) -> None:
        self.hybrid = HybridRetriever()
        self.reranker = None
        self.last_diagnostics: Dict[str, Any] = {}

    def _reranker(self):
        if self.reranker is None:
            from rag.reranker import reranker

            self.reranker = reranker
        return self.reranker

    def close(self) -> None:
        """Release remote retrieval resources owned by this engine."""
        vector = getattr(self.hybrid, "vector", None)
        client = getattr(vector, "client", None)
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def hybrid_query(
        self,
        query: str,
        top_k: int = 10,
        *,
        metadata_filter: RetrievalMetadataFilter | None = None,
    ) -> List[Dict[str, Any]]:
        """仅做混合检索（快速模式）"""
        retrieval_query, rewrite = enhance_query(
            query, enabled=settings.rag_query_expansion_enabled
        )
        results = self.hybrid.retrieve(
            query,
            top_k=top_k,
            lexical_query=retrieval_query,
            metadata_filter=metadata_filter,
        )
        self.last_diagnostics = dict(self.hybrid.last_diagnostics)
        self.last_diagnostics["query_rewrite"] = rewrite
        self.last_diagnostics["rerank"] = {"status": "skipped"}
        return results

    def full_query(
        self,
        query: str,
        top_k: int = 5,
        *,
        metadata_filter: RetrievalMetadataFilter | None = None,
    ) -> List[Dict[str, Any]]:
        """混合检索 + LLM Rerank（精排模式）"""
        started = time.perf_counter()
        multiplier = max(1, settings.rag_rerank_candidates_multiplier)
        retrieval_query, rewrite = enhance_query(
            query, enabled=settings.rag_query_expansion_enabled
        )
        candidates = self.hybrid.retrieve(
            query,
            top_k=top_k * multiplier,
            lexical_query=retrieval_query,
            metadata_filter=metadata_filter,
        )
        self.last_diagnostics = dict(self.hybrid.last_diagnostics)
        self.last_diagnostics["query_rewrite"] = rewrite
        if not settings.rag_rerank_enabled or len(candidates) <= 1:
            self.last_diagnostics["rerank"] = {
                "status": "disabled" if not settings.rag_rerank_enabled else "not_needed",
                "candidate_count": len(candidates),
            }
            return candidates[:top_k]
        confidence = estimate_rerank_confidence(
            candidates,
            self.last_diagnostics,
            top_k=top_k,
        )
        threshold = max(0.0, min(1.0, settings.rag_adaptive_rerank_threshold))
        retrieval_latency_ms = (time.perf_counter() - started) * 1000
        retrieval_budget_ms = settings.rag_rerank_max_retrieval_latency_ms
        latency_budget_exhausted = (
            settings.rag_adaptive_rerank_enabled
            and retrieval_budget_ms > 0
            and retrieval_latency_ms >= retrieval_budget_ms
        )
        if (
            settings.rag_adaptive_rerank_enabled
            and (
                confidence["score"] >= threshold
                or latency_budget_exhausted
            )
        ):
            skip_for_confidence = confidence["score"] >= threshold
            skip_status = (
                "skipped_high_confidence"
                if skip_for_confidence
                else "skipped_latency_budget"
            )
            results = _mark_rerank_skipped(
                candidates, top_k, status=skip_status
            )
            self.last_diagnostics["rerank"] = {
                "status": skip_status,
                "reason": (
                    "hybrid_branches_agree"
                    if skip_for_confidence
                    else "retrieval_latency_budget_exhausted"
                ),
                "candidate_count": len(candidates),
                "result_count": len(results),
                "confidence": confidence,
                "threshold": threshold,
                "retrieval_latency_ms": round(retrieval_latency_ms, 3),
                "retrieval_budget_ms": retrieval_budget_ms,
                "latency_ms": 0.0,
                "end_to_end_latency_ms": round(
                    (time.perf_counter() - started) * 1000, 3
                ),
            }
            return results
        # Rerank against the original user intent; expansions are recall hints.
        rerank_started = time.perf_counter()
        results = self._reranker().rerank(query, candidates, top_k=top_k)
        applied = any(
            (item.get("retrieval") or {}).get("rerank_status") == "applied"
            for item in results
        )
        self.last_diagnostics["rerank"] = {
            "status": "applied" if applied else "fallback",
            "candidate_count": len(candidates),
            "result_count": len(results),
            "reason": (
                "adaptive_confidence_below_threshold"
                if settings.rag_adaptive_rerank_enabled
                else "adaptive_rerank_disabled"
            ),
            "confidence": confidence,
            "threshold": threshold,
            "retrieval_latency_ms": round(retrieval_latency_ms, 3),
            "retrieval_budget_ms": retrieval_budget_ms,
            "latency_ms": round((time.perf_counter() - rerank_started) * 1000, 3),
            "end_to_end_latency_ms": round(
                (time.perf_counter() - started) * 1000, 3
            ),
        }
        return results


query_engine = QueryEngine()
