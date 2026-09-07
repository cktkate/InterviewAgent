from __future__ import annotations

import pytest

from orchestration.budget import (
    AgentBudgetExceeded,
    RuntimeBudget,
    before_llm_call,
    before_tool_call,
    record_llm_tokens,
    runtime_budget,
)
from orchestration.context_engineering import build_planning_context
from rag.query_enhancer import enhance_query
from rag.retrievers.hybrid_retriever import HybridRetriever


def test_runtime_budget_counts_calls_and_tokens():
    with runtime_budget(RuntimeBudget(2, 100, 10)) as usage:
        before_llm_call()
        record_llm_tokens(20, 10)
        before_llm_call()
        snapshot = usage.snapshot()

    assert snapshot["llm_calls"] == 2
    assert snapshot["total_tokens"] == 30


def test_runtime_budget_stops_excess_llm_calls():
    with runtime_budget(RuntimeBudget(1, 100, 10)) as usage:
        before_llm_call()
        with pytest.raises(AgentBudgetExceeded, match="llm_calls"):
            before_llm_call()
        assert usage.snapshot()["budget_exceeded"] is True
        assert usage.snapshot()["budget_dimension"] == "llm_calls"


def test_runtime_budget_stops_excess_tool_calls():
    with runtime_budget(RuntimeBudget(2, 100, 10, max_tool_calls=1)) as usage:
        before_tool_call()
        with pytest.raises(AgentBudgetExceeded, match="tool_calls"):
            before_tool_call()
        assert usage.snapshot()["tool_calls"] == 1
        assert usage.snapshot()["budget_dimension"] == "tool_calls"


def test_context_selector_is_bounded_and_keeps_operational_diagnostics():
    state = {
        "ability_profile": {"RAG": {"mastery": 0.3, "confidence": 0.4}},
        "long_term_weaknesses": ["混合检索"],
        "rag_sources": [
            {"id": "doc-1", "text": "RAG evidence " * 200},
            {"id": "doc-2", "text": "Agent evidence " * 200},
        ],
    }
    context, diagnostics = build_planning_context(state, max_chars=600)

    assert len(context) <= 600
    assert "Historical ability" in context
    assert diagnostics["max_chars"] == 600
    assert diagnostics["rag_source_count"] == 2
    assert diagnostics["estimated_tokens"] > 0
    assert diagnostics["manifest"]
    assert "rag:doc-1" in diagnostics["selected_sources"]
    assert "untrusted_external" in {
        item["trust"] for item in diagnostics["manifest"]
    }
    assert "<retrieved_evidence" in context


def test_token_context_prioritizes_relevant_evidence_and_audits_actions():
    state = {
        "interview_topic": "RAG",
        "jd_parsed": {"tech_stack": ["Weaviate", "BM25"], "core_competencies": []},
        "rag_sources": [
            {"id": "noise", "text": "无关背景材料 " * 500},
            {
                "id": "relevant",
                "text": "RAG 使用 Weaviate 向量召回与 BM25 关键词召回，再通过 RRF 融合。",
                "retrieval": {"matched_branches": ["vector", "bm25"]},
            },
        ],
    }

    context, diagnostics = build_planning_context(state, max_tokens=220)

    assert diagnostics["budget_type"] == "token"
    assert diagnostics["selected_tokens"] <= 220
    assert diagnostics["token_utilization"] <= 1.0
    assert diagnostics["section_budgets"]["Retrieved evidence (untrusted data)"] <= 220
    assert "relevant" in diagnostics["selected_rag_source_ids"]
    assert 'source_id="relevant"' in context
    assert {item["action"] for item in diagnostics["manifest"]} <= {
        "included", "compressed", "dropped"
    }
    assert diagnostics["tokenizer"].startswith(("tiktoken:", "heuristic"))


def test_query_enhancer_expands_known_terms_without_an_llm_call():
    enhanced, diagnostics = enhance_query("RAG 与 MCP 工具调用")

    assert "retrieval" in enhanced
    assert "model" in enhanced
    assert diagnostics["status"] == "expanded"


def test_hybrid_retriever_uses_expansion_only_for_bm25():
    seen = {}

    class Recorder:
        def __init__(self, name):
            self.name = name

        def retrieve(self, query, top_k):
            seen[self.name] = query
            return []

    retriever = HybridRetriever(
        vector=Recorder("vector"), bm25=Recorder("bm25"), timeout_seconds=1
    )
    retriever.retrieve(
        "RAG", top_k=2, lexical_query="RAG retrieval augmented generation"
    )

    assert seen["vector"] == "RAG"
    assert seen["bm25"] == "RAG retrieval augmented generation"
