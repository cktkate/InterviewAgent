from __future__ import annotations

from rag.metadata_filter import (
    RetrievalMetadataFilter,
    apply_progressive_filter,
    normalize_chunk_metadata,
    security_matches,
)
from rag.retrievers.hybrid_retriever import HybridRetriever


def _result(item_id, *, visibility="public", owner_id="", domains=None, points=None):
    return {
        "id": item_id,
        "text": item_id,
        "score": 1.0,
        "source": "vector",
        "metadata": {
            "visibility": visibility,
            "owner_id": owner_id,
            "job_domains": domains or [],
            "knowledge_points": points or [],
            "difficulty": "medium",
        },
    }


def test_metadata_normalization_adds_stable_security_and_domain_fields():
    metadata = normalize_chunk_metadata(
        {"source_path": "knowledge/Agent.md", "tech_stack": ["LangGraph"]},
        text="RAG 与 MCP 工具调用",
    )

    assert metadata["visibility"] == "public"
    assert metadata["owner_id"] == ""
    assert {"ai_agent", "rag"} <= set(metadata["job_domains"])
    assert "langgraph" in metadata["knowledge_points"]


def test_security_filter_allows_public_and_owned_private_only():
    metadata_filter = RetrievalMetadataFilter(requester_id="user-1")

    assert security_matches({"visibility": "public"}, metadata_filter)
    assert security_matches(
        {"visibility": "private", "owner_id": "user-1"}, metadata_filter
    )
    assert not security_matches(
        {"visibility": "private", "owner_id": "user-2"}, metadata_filter
    )


def test_progressive_filter_uses_strict_match_then_can_relax():
    metadata_filter = RetrievalMetadataFilter(
        requester_id="user-1",
        job_domains=("rag",),
        knowledge_points=("rag",),
    )
    results = [
        _result("rag-1", domains=["rag"], points=["rag"]),
        _result("agent-1", domains=["ai_agent"], points=["agent"]),
        _result(
            "private-other",
            visibility="private",
            owner_id="user-2",
            domains=["rag"],
            points=["rag"],
        ),
    ]

    selected, diagnostics = apply_progressive_filter(
        results, metadata_filter, top_k=1
    )
    assert [item["id"] for item in selected] == ["rag-1"]
    assert diagnostics["level"] == "strict"
    assert diagnostics["security_count"] == 2

    selected, diagnostics = apply_progressive_filter(
        results, metadata_filter, top_k=2
    )
    assert [item["id"] for item in selected] == ["rag-1", "agent-1"]
    assert diagnostics["level"] == "security_only"


def test_hybrid_retriever_applies_same_filter_after_rrf():
    class FakeRetriever:
        def __init__(self, source):
            self.source = source

        def retrieve(self, query, top_k, *, metadata_filter=None):
            first = _result("rag", domains=["rag"], points=["rag"])
            second = _result("llm", domains=["llm"], points=["llm"])
            for item in (first, second):
                item["source"] = self.source
            return [first, second]

    retriever = HybridRetriever(
        vector=FakeRetriever("vector"),
        bm25=FakeRetriever("bm25"),
        timeout_seconds=1,
    )
    results = retriever.retrieve(
        "RAG",
        top_k=1,
        metadata_filter=RetrievalMetadataFilter(
            job_domains=("rag",), knowledge_points=("rag",)
        ),
    )

    assert [item["id"] for item in results] == ["rag"]
    assert retriever.last_diagnostics["metadata_filter"]["level"] == "strict"
