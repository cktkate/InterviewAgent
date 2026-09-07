"""
LLM Reranker：使用大模型对候选文档做精排
比传统 cross-encoder 更灵活，能针对具体 query 判断相关性
"""
from __future__ import annotations

from typing import Any, Dict, List

from loguru import logger

from agents.base_agent import BaseAgent
from agents.output_schemas import RerankResult
from orchestration.security_policy import (
    UNTRUSTED_DATA_INSTRUCTION,
    wrap_untrusted_data,
)

RERANK_PROMPT = """你是相关性评估专家。针对用户 Query，对以下候选文档按相关性从高到低排序。
只能使用候选文档中已经存在的 id，不得创建新 id。相关性分数范围为 0 到 1。

Query: {query}

候选文档：
{docs}

输出 JSON 对象：
{{
  "order": ["id1", "id2"],
  "relevance_scores": {{"id1": 0.95, "id2": 0.72}}
}}
"""


class LLMReranker(BaseAgent):
    name = "llm_reranker"

    def rerank(
        self, query: str, candidates: List[Dict[str, Any]], top_k: int = 5
    ) -> List[Dict[str, Any]]:
        if not candidates:
            return []
        docs_text = "\n".join(
            f"[{c['id']}] {c['text'][:300]}" for c in candidates[:20]
        )
        try:
            parsed = self.invoke_llm_json(
                UNTRUSTED_DATA_INSTRUCTION
                + RERANK_PROMPT.format(
                    query=wrap_untrusted_data(query, source="rag_query"),
                    docs=wrap_untrusted_data(docs_text, source="rag_candidates"),
                ),
                "",
                schema=RerankResult,
                # Reranking is optional; a schema miss should immediately fall
                # back to the audited RRF order instead of issuing slow retries.
                max_output_attempts=1,
            )
            ordered_ids = parsed.get("order", [])
            relevance_scores = parsed.get("relevance_scores", {})
        except Exception as e:
            logger.warning("Reranker 失败，保持原序: {}", e)
            fallback = []
            for candidate in candidates[:top_k]:
                item = dict(candidate)
                retrieval = dict(item.get("retrieval") or {})
                retrieval["rerank_status"] = "fallback"
                item["retrieval"] = retrieval
                fallback.append(item)
            return fallback

        id_to_doc = {str(c["id"]): c for c in candidates}
        reranked: List[Dict[str, Any]] = []
        seen = set()
        for rid in (str(item) for item in ordered_ids):
            if rid in id_to_doc and rid not in seen:
                reranked.append(id_to_doc[rid])
                seen.add(rid)
        # 兜底：补充未被排序的
        for c in candidates:
            if str(c["id"]) not in seen:
                reranked.append(c)
                seen.add(str(c["id"]))

        output = []
        for rank, candidate in enumerate(reranked[:top_k], start=1):
            item = dict(candidate)
            rid = str(item["id"])
            raw_relevance = relevance_scores.get(rid)
            rerank_score = None
            if isinstance(raw_relevance, (int, float)):
                rerank_score = max(0.0, min(1.0, float(raw_relevance)))
            retrieval = dict(item.get("retrieval") or {})
            retrieval.update(
                {
                    "rerank_status": "applied",
                    "rerank_rank": rank,
                    "rerank_score": rerank_score,
                }
            )
            item["retrieval"] = retrieval
            item["rerank_score"] = rerank_score
            output.append(item)
        return output
    

    # def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
    #     query = state.get("query", "")
    #     candidates = state.get("candidates", [])
    #     top_k = state.get("top_k", 5)

    #     reranked = self.rerank(query, candidates, top_k)

    #     return {
    #         **state,
    #         "candidates": reranked,
    #     }


    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        # reranker 不走 graph node，这里做兼容占位
        return state

reranker = LLMReranker()
