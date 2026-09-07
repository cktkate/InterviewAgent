from llama_index.core import Document

from rag.indexer import (
    deterministic_chunk_id,
    split_documents,
    weaviate_uuid_for_chunk,
)
from rag.query_engine import QueryEngine, estimate_rerank_confidence
from rag.reranker import LLMReranker
from rag.retrievers.hybrid_retriever import HybridRetriever, reciprocal_rank_fusion


def _result(chunk_id, score, source):
    return {
        "id": chunk_id,
        "text": f"text-{chunk_id}",
        "metadata": {"chunk_id": chunk_id},
        "score": score,
        "source": source,
    }


def test_chunk_ids_and_weaviate_uuids_are_deterministic():
    first = deterministic_chunk_id("knowledge/Agent.md", 0, "Agent runtime state")
    second = deterministic_chunk_id("knowledge/Agent.md", 0, "Agent   runtime state")

    assert first == second
    assert weaviate_uuid_for_chunk(first) == weaviate_uuid_for_chunk(second)
    assert deterministic_chunk_id("knowledge/Agent.md", 1, "Agent runtime state") != first


def test_document_splitting_persists_the_same_chunk_id_in_metadata():
    docs = [
        Document(
            text="Agent runtime state and checkpoint recovery.",
            metadata={"source_path": "knowledge/Agent.md"},
        )
    ]

    first = split_documents(docs, chunk_size=64)
    second = split_documents(docs, chunk_size=64)

    assert first[0]["id"] == second[0]["id"]
    assert first[0]["metadata"]["chunk_id"] == first[0]["id"]
    assert first[0]["metadata"]["source_path"] == "knowledge/Agent.md"


def test_rrf_really_fuses_the_same_chunk_and_keeps_evidence():
    results = reciprocal_rank_fusion(
        [_result("shared", 0.91, "vector"), _result("vector-only", 0.8, "vector")],
        [_result("shared", 8.4, "bm25"), _result("bm25-only", 6.2, "bm25")],
    )

    shared = next(item for item in results if item["id"] == "shared")
    assert len(results) == 3
    assert shared["source"] == "hybrid"
    assert shared["retrieval"]["vector_rank"] == 1
    assert shared["retrieval"]["bm25_rank"] == 1
    assert shared["retrieval"]["matched_branches"] == ["bm25", "vector"]


def test_rrf_fuses_same_passage_when_indexes_have_different_ids():
    vector = _result("vector-id", 0.91, "vector")
    bm25 = _result("legacy-bm25-id", 8.4, "bm25")
    vector["text"] = bm25["text"] = "Redis session state and checkpoint"
    vector["metadata"] = {"source_path": "knowledge/Agent.md"}
    bm25["metadata"] = {"file_name": "Agent.md"}

    results = reciprocal_rank_fusion([vector], [bm25])

    assert len(results) == 1
    assert results[0]["id"] == "vector-id"
    assert results[0]["source"] == "hybrid"
    assert results[0]["retrieval"]["vector_id"] == "vector-id"
    assert results[0]["retrieval"]["bm25_id"] == "legacy-bm25-id"


def test_hybrid_retriever_degrades_to_healthy_branch():
    class FailedVector:
        def retrieve(self, query, top_k):
            raise TimeoutError("vector unavailable")

    class HealthyBM25:
        def retrieve(self, query, top_k):
            return [_result("bm25", 3.0, "bm25")]

    retriever = HybridRetriever(
        vector=FailedVector(),
        bm25=HealthyBM25(),
        timeout_seconds=1,
    )
    results = retriever.retrieve("agent state", top_k=5)

    assert [item["id"] for item in results] == ["bm25"]
    assert results[0]["source"] == "bm25"
    assert retriever.last_diagnostics["vector"]["status"] == "failed"
    assert retriever.last_diagnostics["bm25"]["status"] == "success"


def test_reranker_uses_object_schema_and_preserves_rrf_evidence(monkeypatch):
    agent = LLMReranker.__new__(LLMReranker)
    calls = {}

    def fake_invoke(*args, **kwargs):
        calls.update(kwargs)
        return {
            "order": ["b", "a", "unknown"],
            "relevance_scores": {"b": 0.95, "a": 0.7, "unknown": 1.0},
        }

    monkeypatch.setattr(agent, "invoke_llm_json", fake_invoke)
    candidates = [
        {**_result("a", 0.03, "hybrid"), "retrieval": {"rrf_rank": 1}},
        {**_result("b", 0.02, "hybrid"), "retrieval": {"rrf_rank": 2}},
    ]
    results = agent.rerank("query", candidates, top_k=2)

    assert [item["id"] for item in results] == ["b", "a"]
    assert results[0]["retrieval"]["rrf_rank"] == 2
    assert results[0]["retrieval"]["rerank_rank"] == 1
    assert results[0]["rerank_score"] == 0.95
    assert calls["max_output_attempts"] == 1


def test_full_query_uses_reranker_when_enabled(monkeypatch):
    class FakeHybrid:
        def __init__(self):
            self.last_diagnostics = {"vector": {"status": "success", "count": 2}}
            self.requested_top_k = None

        def retrieve(self, query, top_k, *, lexical_query=None, metadata_filter=None):
            self.requested_top_k = top_k
            self.lexical_query = lexical_query
            self.metadata_filter = metadata_filter
            return [_result("a", 0.03, "hybrid"), _result("b", 0.02, "hybrid")]

    class FakeReranker:
        def rerank(self, query, candidates, top_k):
            result = dict(candidates[1])
            result["retrieval"] = {"rerank_status": "applied"}
            return [result]

    import rag.query_engine as query_engine_module

    monkeypatch.setattr(query_engine_module.settings, "rag_rerank_enabled", True)
    monkeypatch.setattr(query_engine_module.settings, "rag_rerank_candidates_multiplier", 3)
    monkeypatch.setattr(query_engine_module.settings, "rag_query_expansion_enabled", True)
    engine = QueryEngine.__new__(QueryEngine)
    engine.hybrid = FakeHybrid()
    engine.reranker = FakeReranker()
    engine.last_diagnostics = {}

    results = engine.full_query("agent", top_k=2)

    assert engine.hybrid.requested_top_k == 6
    assert "agentic" in engine.hybrid.lexical_query
    assert [item["id"] for item in results] == ["b"]
    assert engine.last_diagnostics["rerank"]["status"] == "applied"


def test_adaptive_rerank_skips_llm_when_both_branches_strongly_agree(monkeypatch):
    candidates = []
    for rank in range(1, 4):
        item = _result(f"doc-{rank}", 0.03 - rank * 0.001, "hybrid")
        item["retrieval"] = {
            "matched_branches": ["bm25", "vector"],
            "vector_rank": rank,
            "bm25_rank": rank,
        }
        candidates.append(item)

    class FakeHybrid:
        last_diagnostics = {
            "vector": {"status": "success", "count": 3},
            "bm25": {"status": "success", "count": 3},
        }

        def retrieve(self, *args, **kwargs):
            return candidates

    class ForbiddenReranker:
        def rerank(self, *args, **kwargs):
            raise AssertionError("high-confidence retrieval must skip the LLM")

    import rag.query_engine as query_engine_module

    monkeypatch.setattr(query_engine_module.settings, "rag_rerank_enabled", True)
    monkeypatch.setattr(query_engine_module.settings, "rag_adaptive_rerank_enabled", True)
    monkeypatch.setattr(query_engine_module.settings, "rag_adaptive_rerank_threshold", 0.78)
    engine = QueryEngine.__new__(QueryEngine)
    engine.hybrid = FakeHybrid()
    engine.reranker = ForbiddenReranker()
    engine.last_diagnostics = {}

    results = engine.full_query("checkpoint recovery", top_k=2)

    assert [item["id"] for item in results] == ["doc-1", "doc-2"]
    assert engine.last_diagnostics["rerank"]["status"] == "skipped_high_confidence"
    assert engine.last_diagnostics["rerank"]["confidence"]["score"] >= 0.78
    assert results[0]["retrieval"]["rerank_status"] == "skipped_high_confidence"


def test_adaptive_rerank_keeps_llm_for_conflicting_branches(monkeypatch):
    first = _result("vector-only", 0.03, "vector")
    first["retrieval"] = {
        "matched_branches": ["vector"],
        "vector_rank": 1,
    }
    second = _result("bm25-only", 0.029, "bm25")
    second["retrieval"] = {
        "matched_branches": ["bm25"],
        "bm25_rank": 1,
    }

    class FakeHybrid:
        last_diagnostics = {
            "vector": {"status": "success", "count": 2},
            "bm25": {"status": "success", "count": 2},
        }

        def retrieve(self, *args, **kwargs):
            return [first, second]

    class RecordingReranker:
        called = False

        def rerank(self, query, candidates, top_k):
            self.called = True
            result = dict(candidates[1])
            result["retrieval"] = {
                **result["retrieval"],
                "rerank_status": "applied",
            }
            return [result]

    import rag.query_engine as query_engine_module

    monkeypatch.setattr(query_engine_module.settings, "rag_rerank_enabled", True)
    monkeypatch.setattr(query_engine_module.settings, "rag_adaptive_rerank_enabled", True)
    monkeypatch.setattr(query_engine_module.settings, "rag_adaptive_rerank_threshold", 0.78)
    reranker = RecordingReranker()
    engine = QueryEngine.__new__(QueryEngine)
    engine.hybrid = FakeHybrid()
    engine.reranker = reranker
    engine.last_diagnostics = {}

    results = engine.full_query("checkpoint recovery", top_k=2)

    assert reranker.called is True
    assert results[0]["id"] == "bm25-only"
    assert engine.last_diagnostics["rerank"]["status"] == "applied"
    assert engine.last_diagnostics["rerank"]["reason"] == "adaptive_confidence_below_threshold"


def test_adaptive_rerank_respects_retrieval_latency_budget(monkeypatch):
    first = _result("vector-only", 0.03, "vector")
    first["retrieval"] = {"matched_branches": ["vector"], "vector_rank": 1}
    second = _result("bm25-only", 0.029, "bm25")
    second["retrieval"] = {"matched_branches": ["bm25"], "bm25_rank": 1}

    class SlowHybrid:
        last_diagnostics = {
            "vector": {"status": "success", "count": 1},
            "bm25": {"status": "success", "count": 1},
        }

        def retrieve(self, *args, **kwargs):
            return [first, second]

    class ForbiddenReranker:
        def rerank(self, *args, **kwargs):
            raise AssertionError("latency budget exhaustion must skip the LLM")

    import rag.query_engine as query_engine_module

    ticks = iter([10.0, 15.0, 15.0])
    monkeypatch.setattr(query_engine_module.time, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(query_engine_module.settings, "rag_rerank_enabled", True)
    monkeypatch.setattr(query_engine_module.settings, "rag_adaptive_rerank_enabled", True)
    monkeypatch.setattr(query_engine_module.settings, "rag_adaptive_rerank_threshold", 0.99)
    monkeypatch.setattr(
        query_engine_module.settings, "rag_rerank_max_retrieval_latency_ms", 3000.0
    )
    engine = QueryEngine.__new__(QueryEngine)
    engine.hybrid = SlowHybrid()
    engine.reranker = ForbiddenReranker()
    engine.last_diagnostics = {}

    results = engine.full_query("checkpoint recovery", top_k=2)

    assert [item["id"] for item in results] == ["vector-only", "bm25-only"]
    assert engine.last_diagnostics["rerank"]["status"] == "skipped_latency_budget"
    assert engine.last_diagnostics["rerank"]["reason"] == "retrieval_latency_budget_exhausted"


def test_rerank_confidence_rejects_single_branch_degradation():
    candidate = _result("only", 0.03, "vector")
    candidate["retrieval"] = {
        "matched_branches": ["vector"],
        "vector_rank": 1,
    }
    confidence = estimate_rerank_confidence(
        [candidate],
        {
            "vector": {"status": "success"},
            "bm25": {"status": "failed"},
        },
        top_k=1,
    )

    assert confidence["branch_health"] == 0.0
    assert confidence["score"] < 0.78


def test_interview_rag_node_uses_full_pipeline(monkeypatch):
    import rag.query_engine as query_engine_module
    from orchestration.nodes import node_rag_retrieve

    class FakeQueryEngine:
        last_diagnostics = {"rerank": {"status": "applied"}}

        def full_query(self, query, top_k, *, metadata_filter=None):
            self.metadata_filter = metadata_filter
            return [{"id": "chunk", "text": "Agent state context", "score": 1.0}]

        def hybrid_query(self, query, top_k):
            raise AssertionError("main interview flow must use full_query")

    monkeypatch.setattr(query_engine_module, "query_engine", FakeQueryEngine())
    result = node_rag_retrieve(
        {
            "interview_topic": "Agent",
            "jd_parsed": {"tech_stack": ["LangGraph"], "core_competencies": []},
        }
    )

    assert result["rag_context"] == "Agent state context"
    assert result["rag_sources"][0]["id"] == "chunk"
    assert result["rag_diagnostics"]["rerank"]["status"] == "applied"
    assert query_engine_module.query_engine.metadata_filter.requester_id == ""
