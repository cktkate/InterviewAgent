"""Deterministic tests for RAG and Agent offline evaluation metrics."""
from __future__ import annotations

import json

from evals.agent_eval import (
    AgentTraceCase,
    build_case_template,
    collect_bad_case_templates,
    evaluate_agent_case,
    load_trace_groups,
    run_agent_evaluation,
)
from evals.common import evaluate_gates
from evals.rag_eval import EvidenceLabel, RAGCase, ranking_metrics, run_rag_evaluation
from rag.retrievers import bm25_retriever


def test_rag_metrics_use_rank_and_graded_relevance():
    case = RAGCase(
        case_id="rag-1",
        query="agent memory",
        relevant_ids=["d2", "d3"],
        relevance_grades={"d2": 2.0, "d3": 1.0},
    )
    results = [{"id": "d1"}, {"id": "d2"}, {"id": "d3"}]
    metrics = ranking_metrics(results, case, k=3)
    assert metrics["recall_at_k"] == 1.0
    assert metrics["hit_rate"] == 1.0
    assert metrics["mrr"] == 0.5
    assert 0.0 < metrics["ndcg_at_k"] < 1.0


def test_rag_source_labels_support_absolute_paths():
    case = RAGCase(
        case_id="rag-source",
        query="MCP",
        relevant_sources=["Agent.md"],
    )
    results = [{
        "id": "chunk-1",
        "metadata": {"file_path": "D:/repo/rag/knowledge/Agent.md"},
    }]
    metrics = ranking_metrics(results, case, k=1)
    assert metrics["recall_at_k"] == 1.0
    assert metrics["mrr"] == 1.0


def test_rag_evidence_labels_survive_chunk_id_changes_and_distinguish_rank():
    case = RAGCase(
        case_id="rag-evidence",
        query="how does agent memory recover",
        relevant_evidence=[
            EvidenceLabel(
                evidence_id="redis",
                source="Agent.md",
                all_terms=["Redis", "TTL"],
                grade=2,
            ),
            EvidenceLabel(
                evidence_id="mysql",
                source="Agent.md",
                all_terms=["MySQL", "长期记忆"],
                grade=1,
            ),
        ],
    )
    results = [
        {"id": "unrelated", "text": "RAG rerank", "metadata": {"file_name": "Agent.md"}},
        {"id": "new-id-2", "text": "MySQL 保存长期记忆", "metadata": {"file_name": "Agent.md"}},
        {"id": "new-id-1", "text": "Redis 使用 TTL", "metadata": {"file_name": "Agent.md"}},
    ]
    metrics = ranking_metrics(results, case, k=3)
    assert metrics["recall_at_k"] == 1.0
    assert metrics["mrr"] == 0.5
    assert 0.0 < metrics["ndcg_at_k"] < 1.0


def test_rag_report_includes_stratified_tag_metrics():
    cases = [
        RAGCase(case_id="agent", query="a", relevant_ids=["a"], tags=["agent", "easy"]),
        RAGCase(case_id="llm", query="b", relevant_ids=["b"], tags=["llm", "hard"]),
    ]
    report = run_rag_evaluation(
        cases,
        retrieve_fn=lambda query, _: [{"id": query}],
        top_k=1,
    )
    assert report["metrics_by_tag"]["agent"]["case_count"] == 1
    assert report["metrics_by_tag"]["hard"]["hit_rate"] == 1.0


def test_bm25_fallback_tokenizes_chinese_without_jieba(monkeypatch):
    monkeypatch.setattr(bm25_retriever, "jieba", None)
    query_tokens = bm25_retriever._tokenize("数据清洗与低质量样本")
    document_tokens = bm25_retriever._tokenize("过滤低质量数据并完成清洗")
    assert "数据" in query_tokens
    assert "清洗" in query_tokens
    assert {"数据", "清洗", "低质"} <= set(query_tokens) & set(document_tokens)


def test_rag_runner_records_failures_without_stopping_suite():
    cases = [
        RAGCase(case_id="ok", query="ok", relevant_ids=["d1"]),
        RAGCase(case_id="bad", query="bad", relevant_ids=["d2"]),
    ]

    def retrieve(query, top_k):
        if query == "bad":
            raise RuntimeError("retriever down")
        return [{"id": "d1"}]

    report = run_rag_evaluation(cases, retrieve_fn=retrieve, top_k=5)
    assert report["metrics"]["case_count"] == 2
    assert report["metrics"]["retrieval_success_rate"] == 0.5
    assert report["metrics"]["hit_rate"] == 0.5
    assert report["cases"][1]["status"] == "failed"


def _span(trace_id, name, start, *, status="ok", attributes=None, duration=10.0):
    return {
        "trace_id": trace_id,
        "span_id": f"span-{start}",
        "parent_span_id": None,
        "name": name,
        "status": status,
        "start_time_unix_nano": start,
        "duration_ms": duration,
        "attributes": attributes or {},
        "events": [],
    }


def _successful_trace(trace_id="trace-1"):
    return [
        _span(trace_id, "agent.node.route", 1, attributes={"agent.node.name": "route"}),
        _span(trace_id, "agent.node.load_memory", 2, attributes={"agent.node.name": "load_memory"}),
        _span(trace_id, "agent.tool.github.search_learning_repos", 3, attributes={
            "agent.tool.name": "github.search_learning_repos"
        }),
        _span(trace_id, "agent.node.ask", 4, attributes={"agent.node.name": "ask"}),
        _span(trace_id, "agent.run.start_interview", 5, attributes={
            "agent.run.operation": "start_interview",
            "agent.session.id": "session-1",
            "agent.run.output.runtime_llm_calls": 3,
            "agent.run.output.runtime_tool_calls": 1,
            "agent.run.output.runtime_total_tokens": 1200,
            "agent.run.output.security_event_count": 1,
            "agent.run.output.collaboration_status": "adopted",
            "agent.run.output.planning_attempt": 2,
            "agent.run.output.planning_action_count": 1,
            "agent.run.output.planning_decision": "accepted",
            "agent.run.output.context_selected_tokens": 900,
            "agent.run.output.context_max_tokens": 1800,
            "agent.run.output.context_token_utilization": 0.5,
            "agent.run.output.context_selected_rag_source_count": 3,
            "agent.run.output.context_compressed_source_count": 1,
            "agent.run.output.context_dropped_source_count": 2,
        }, duration=100.0),
    ]


def test_agent_trajectory_checks_path_terminal_tools_and_loops():
    spans = _successful_trace()
    groups = {"trace-1": spans}
    case = AgentTraceCase(
        case_id="agent-1",
        trace_id="trace-1",
        expected_nodes=["route", "load_memory", "ask"],
        required_nodes=["route", "ask"],
        forbidden_nodes=["evaluate_one"],
        expected_terminal="ask",
        expected_tools=["github.search_learning_repos"],
        default_max_node_visits=1,
        max_duration_ms=200,
        max_llm_calls=5,
        max_tool_calls=2,
        max_total_tokens=2000,
        max_planning_attempts=2,
        allowed_planning_decisions=["accepted", "accepted_with_warnings"],
        require_collaboration_review=True,
    )
    result = evaluate_agent_case(case, groups)
    assert result["passed"] is True
    assert result["exact_path_match"] is True
    assert result["expected_tool_recall"] == 1.0
    assert result["llm_budget_pass"] is True
    assert result["token_budget_pass"] is True
    assert result["security_signal_count"] == 1
    assert result["tool_budget_pass"] is True
    assert result["planning_attempt_pass"] is True
    assert result["planning_decision_pass"] is True
    assert result["collaboration_reviewed"] is True
    assert result["context_selected_tokens"] == 900
    assert result["context_token_utilization"] == 0.5

    bad_case = case.model_copy(update={
        "case_id": "agent-bad",
        "forbidden_nodes": ["load_memory"],
    })
    bad = evaluate_agent_case(bad_case, groups)
    assert bad["passed"] is False
    assert bad["forbidden_hits"] == ["load_memory"]


def test_agent_trace_loader_selection_and_template(tmp_path):
    rows = _successful_trace("old") + _successful_trace("new")
    rows[-1]["start_time_unix_nano"] = 100
    trace_file = tmp_path / "traces.jsonl"
    trace_file.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    groups = load_trace_groups(str(trace_file))
    case = AgentTraceCase(
        case_id="latest",
        operation="start_interview",
        required_nodes=["route", "ask"],
        expected_terminal="ask",
    )
    result = evaluate_agent_case(case, groups)
    assert result["trace_id"] == "new"

    template = build_case_template("new", groups["new"])
    assert template["expected_nodes"] == ["route", "load_memory", "ask"]
    assert "needs-human-review" in template["tags"]

    report = run_agent_evaluation([case], groups)
    assert report["metrics"]["task_success_rate"] == 1.0
    assert report["metrics"]["average_llm_calls"] == 3.0
    assert report["metrics"]["average_tool_calls"] == 1.0
    assert report["metrics"]["planning_replan_rate"] == 1.0
    assert report["metrics"]["planning_evidence_action_rate"] == 1.0
    assert report["metrics"]["collaboration_review_rate"] == 1.0
    assert report["metrics"]["average_context_tokens"] == 900.0
    assert report["metrics"]["average_context_selected_rag_sources"] == 3.0


def test_regression_gates_support_absolute_and_baseline_rules():
    gate = evaluate_gates(
        {"recall_at_k": 0.80, "p95_latency_ms": 1200},
        {
            "min": {"recall_at_k": 0.75},
            "max": {"p95_latency_ms": 1500},
            "max_drop": {"recall_at_k": 0.02},
        },
        {"recall_at_k": 0.85},
    )
    assert gate["passed"] is False
    assert gate["violations"][0]["rule"] == "max_drop"


def test_bad_case_collector_extracts_slow_and_degraded_traces():
    spans = _successful_trace("slow")
    root = spans[-1]
    root["duration_ms"] = 40000.0
    root["attributes"]["agent.run.output.collaboration_status"] = (
        "adopted_with_degradation"
    )

    cases = collect_bad_case_templates(
        {"slow": spans}, max_duration_ms=30000.0
    )

    assert len(cases) == 1
    assert "latency_threshold_exceeded" in cases[0]["bad_case_reasons"]
    assert "collaboration_degraded" in cases[0]["bad_case_reasons"]
    assert "needs-human-review" in cases[0]["tags"]
    assert AgentTraceCase.model_validate(cases[0]).case_id == "bad-slow"
