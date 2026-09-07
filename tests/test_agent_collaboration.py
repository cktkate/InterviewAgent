from __future__ import annotations

from orchestration.collaboration import (
    InterviewCoordinatorAgent,
    QuestionReviewerAgent,
    submit_artifact,
)
from orchestration.adaptive_policy import append_next_question, initialize_candidate_pool
from orchestration.planning_loop import execute_evidence_requests
from tool_runtime.core import ToolResult


def _question(question_id: str, prompt: str, knowledge_point: str = "RAG"):
    return {
        "id": question_id,
        "topic": knowledge_point,
        "knowledge_point": knowledge_point,
        "prompt": prompt,
        "focus": knowledge_point,
        "difficulty": "medium",
        "expected_points": ["原理", "工程实践"],
        "job_weight": 0.8,
    }


def test_coordinator_declares_collaboration_dependencies():
    state = {}
    submit_artifact(
        state,
        task_type="resume_analysis",
        agent_name="resume_analyzer",
        artifact_type="candidate_profile",
        content={"match_level": "high"},
    )
    coordinator = InterviewCoordinatorAgent()

    coordinator.prepare_question_planning(state)

    tasks = {item["task_type"]: item for item in state["collaboration"]["tasks"]}
    assert set(tasks) == {
        "jd_analysis",
        "resume_analysis",
        "rag_retrieval",
        "question_planning",
        "question_review",
    }
    assert tasks["question_planning"]["dependencies"] == [
        "jd_analysis",
        "resume_analysis",
        "rag_retrieval",
    ]


def test_submit_artifact_completes_task_and_replaces_previous_result():
    state = {}
    submit_artifact(
        state,
        task_type="jd_analysis",
        agent_name="jd_analyzer",
        artifact_type="job_profile",
        content={"title": "AI Engineer"},
    )
    submit_artifact(
        state,
        task_type="jd_analysis",
        agent_name="jd_analyzer",
        artifact_type="job_profile",
        content={"title": "AI Agent Engineer"},
    )

    board = state["collaboration"]
    assert board["tasks"][0]["status"] == "completed"
    assert len(board["artifacts"]) == 1
    assert board["artifacts"][0]["content"]["title"] == "AI Agent Engineer"


def test_reviewer_filters_semantic_duplicates_when_budget_remains_available():
    state = {
        "total_questions": 2,
        "interview_topic": "RAG",
        "jd_parsed": {"tech_stack": ["RAG"], "core_competencies": []},
        "rag_sources": [{"id": "doc-1", "text": "RAG 检索与生成的工程实践"}],
        "question_candidates": [
            _question("Q1", "请解释 RAG 的检索与生成流程"),
            _question("Q2", "请解释 RAG 的检索与生成流程"),
            _question("Q3", "如何评估 RAG 系统的召回效果"),
        ],
        "question_plan": [],
    }

    result = QuestionReviewerAgent().run(state)

    assert result["question_review"]["rejected_count"] == 1
    assert result["question_review"]["filter_applied"] is True
    assert [item["id"] for item in result["question_candidates"]] == ["Q1", "Q3"]
    assert result["question_plan"][0]["id"] in {"Q1", "Q3"}
    assert result["question_candidates"][0]["evidence_ids"] == ["doc-1"]
    assert result["question_candidates"][0]["grounded"] is True


def test_reviewer_only_binds_evidence_visible_in_context_manifest():
    state = {
        "total_questions": 1,
        "interview_topic": "RAG",
        "jd_parsed": {"tech_stack": ["RAG"], "core_competencies": []},
        "rag_sources": [
            {"id": "dropped", "text": "RAG 检索材料"},
            {"id": "selected", "text": "RAG 检索与排序材料"},
        ],
        "context_diagnostics": {"selected_rag_source_ids": ["selected"]},
        "question_candidates": [_question("Q1", "请解释 RAG 检索与排序")],
        "question_plan": [],
    }

    result = QuestionReviewerAgent().run(state)

    assert result["question_candidates"][0]["evidence_ids"] == ["selected"]
    assert result["question_review"]["reviews"][0]["evidence_source_count"] == 1


def test_reviewer_fails_open_when_filter_would_exhaust_question_budget():
    state = {
        "total_questions": 3,
        "interview_topic": "RAG",
        "jd_parsed": {"tech_stack": ["RAG"], "core_competencies": []},
        "rag_sources": [],
        "question_candidates": [
            _question("Q1", "请解释 RAG 的检索与生成流程"),
            _question("Q2", "请解释 RAG 的检索与生成流程"),
            _question("Q3", "如何评估 RAG 系统的召回效果"),
        ],
        "question_plan": [_question("Q1", "请解释 RAG 的检索与生成流程")],
    }

    result = QuestionReviewerAgent().run(state)

    assert result["question_review"]["filter_applied"] is False
    assert result["question_review"]["decision"] == "revise"
    assert result["question_review"]["requires_replan"] is True
    assert len(result["question_candidates"]) == 3
    assert result["collaboration"]["artifacts"][0]["artifact_type"] == "question_review"


def test_planning_evidence_action_injects_identity_and_deduplicates():
    calls = []

    class FakeExecutor:
        def execute(self, name, arguments, *, context, idempotency_key):
            calls.append((name, arguments, context, idempotency_key))
            return ToolResult(
                ok=True,
                tool_call_id="call-1",
                tool_name=name,
                status="success",
                data=[{"id": "doc-1", "text": "RAG production evidence"}],
                attempts=1,
                duration_ms=1.2,
                audit={"risk_level": "read_only"},
            )

    state = {
        "user_id": "user-1",
        "session_id": "session-1",
        "interview_topic": "RAG",
        "jd_parsed": {"title": "AI Agent", "tech_stack": ["RAG"]},
        "rag_sources": [],
    }
    request = {
        "action": "rag_retrieve",
        "query": "RAG production evaluation",
        "reason": "missing production evidence",
        "requester_id": "spoofed-user",
    }

    assert execute_evidence_requests(
        state, [request], max_actions=2, executor=FakeExecutor()
    ) == 1
    assert execute_evidence_requests(
        state, [request], max_actions=2, executor=FakeExecutor()
    ) == 0
    assert calls[0][0] == "rag.retrieve_interview_evidence"
    assert calls[0][1]["requester_id"] == "user-1"
    assert calls[0][2].permissions == frozenset({"knowledge:read"})
    assert state["rag_sources"][0]["id"] == "doc-1"
    assert state["planning_actions"][-1]["error_code"] == "duplicate_action"


def test_node_planning_replans_after_reviewer_rejection(monkeypatch):
    import orchestration.nodes as nodes

    class FakePlanner:
        def __init__(self):
            self.calls = 0

        def run(self, state):
            self.calls += 1
            if self.calls == 1:
                candidates = [
                    _question("Q1", "请解释 RAG 的检索流程"),
                    _question("Q2", "请解释 RAG 的检索流程"),
                ]
            else:
                candidates = [
                    _question("Q1", "请解释 RAG 的检索流程"),
                    _question("Q2", "如何评估 RAG 的召回质量"),
                ]
            state["planning_evidence_requests"] = []
            initialize_candidate_pool(state, candidates)
            append_next_question(state)
            return state

    planner = FakePlanner()
    monkeypatch.setattr(nodes, "_question_planner", planner)
    monkeypatch.setattr(nodes.settings, "agent_planning_max_attempts", 2)
    state = {
        "total_questions": 2,
        "interview_topic": "RAG",
        "jd_parsed": {"title": "AI Agent", "tech_stack": ["RAG"]},
        "rag_sources": [{"id": "doc-1", "text": "RAG 检索、召回与评估"}],
    }

    result = nodes.node_plan_questions(state)

    assert planner.calls == 2
    assert result["planning_history"][0]["review_decision"] == "revise"
    assert result["planning_history"][1]["review_decision"] in {
        "accepted", "accepted_with_warnings"
    }
    assert result["planning_feedback"]
    assert result["planning_decision"] in {"accepted", "accepted_with_warnings"}
    assert result["collaboration"]["decision"]["status"].startswith("adopted")
