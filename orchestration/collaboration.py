"""Lightweight, auditable collaboration primitives for interview agents.

The board is intentionally stored as plain JSON-compatible data in
``InterviewState`` so LangGraph checkpoints can persist it without custom
serializers.  LLMs produce content; the coordinator and reviewer keep control
flow deterministic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence
import uuid

from opentelemetry.trace import Status, StatusCode

from observability.tracing import get_tracer
from orchestration.adaptive_policy import append_next_question, initialize_candidate_pool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _board(state: MutableMapping[str, Any]) -> Dict[str, Any]:
    board = dict(state.get("collaboration") or {})
    board.setdefault("board_id", str(uuid.uuid4()))
    board.setdefault("tasks", [])
    board.setdefault("artifacts", [])
    board.setdefault("events", [])
    state["collaboration"] = board
    return board


def _find_task(board: Mapping[str, Any], task_type: str) -> Dict[str, Any] | None:
    for task in board.get("tasks") or []:
        if task.get("task_type") == task_type:
            return task
    return None


def ensure_task(
    state: MutableMapping[str, Any],
    *,
    task_type: str,
    assignee: str,
    dependencies: Sequence[str] = (),
) -> Dict[str, Any]:
    board = _board(state)
    existing = _find_task(board, task_type)
    if existing is not None:
        existing["assignee"] = assignee
        merged_dependencies = list(existing.get("dependencies") or [])
        for dependency in dependencies:
            if dependency not in merged_dependencies:
                merged_dependencies.append(dependency)
        existing["dependencies"] = merged_dependencies
        return existing
    task = {
        "task_id": str(uuid.uuid4()),
        "task_type": task_type,
        "assignee": assignee,
        "status": "pending",
        "dependencies": list(dependencies),
        "retry_count": 0,
        "created_at": _now(),
    }
    board["tasks"].append(task)
    board["events"].append({
        "event": "task_published",
        "task_id": task["task_id"],
        "agent": assignee,
        "at": _now(),
    })
    state["collaboration"] = board
    return task


def submit_artifact(
    state: MutableMapping[str, Any],
    *,
    task_type: str,
    agent_name: str,
    artifact_type: str,
    content: Mapping[str, Any],
    confidence: float = 1.0,
    evidence: Iterable[str] = (),
) -> Dict[str, Any]:
    task = ensure_task(state, task_type=task_type, assignee=agent_name)
    board = _board(state)
    artifact = {
        "artifact_id": str(uuid.uuid4()),
        "task_id": task["task_id"],
        "agent_name": agent_name,
        "artifact_type": artifact_type,
        "content": dict(content),
        "confidence": max(0.0, min(1.0, float(confidence))),
        "evidence": [str(item) for item in evidence if str(item)],
        "created_at": _now(),
    }
    # One latest artifact per task keeps checkpoint growth bounded.
    board["artifacts"] = [
        item for item in board["artifacts"] if item.get("task_id") != task["task_id"]
    ]
    board["artifacts"].append(artifact)
    task["status"] = "completed"
    task["completed_at"] = _now()
    board["events"].append({
        "event": "artifact_submitted",
        "task_id": task["task_id"],
        "artifact_id": artifact["artifact_id"],
        "agent": agent_name,
        "at": _now(),
    })
    # Keep only recent operational events in long-running restored sessions.
    board["events"] = board["events"][-50:]
    state["collaboration"] = board
    return artifact


def set_task_status(
    state: MutableMapping[str, Any],
    *,
    task_type: str,
    assignee: str,
    status: str,
    reason: str = "",
) -> Dict[str, Any]:
    if status not in {"pending", "running", "completed", "failed", "skipped"}:
        raise ValueError(f"unsupported collaboration task status: {status}")
    task = ensure_task(state, task_type=task_type, assignee=assignee)
    board = _board(state)
    task["status"] = status
    task["updated_at"] = _now()
    if reason:
        task["reason"] = str(reason)[:120]
    board["events"].append({
        "event": f"task_{status}",
        "task_id": task["task_id"],
        "agent": assignee,
        "reason": str(reason)[:120],
        "at": _now(),
    })
    board["events"] = board["events"][-50:]
    state["collaboration"] = board
    return task


class InterviewCoordinatorAgent:
    """Deterministic coordinator that declares dependencies and adopts outputs."""

    name = "interview_coordinator"

    def prepare_question_planning(self, state: MutableMapping[str, Any]) -> None:
        tracer = get_tracer()
        with tracer.start_as_current_span("agent.collaboration.coordinate") as span:
            ensure_task(state, task_type="jd_analysis", assignee="jd_analyzer")
            board = _board(state)
            has_resume = bool(
                str(state.get("resume_text") or "").strip()
                or state.get("resume_parsed")
                or _find_task(board, "resume_analysis") is not None
            )
            if has_resume:
                ensure_task(
                    state,
                    task_type="resume_analysis",
                    assignee="resume_analyzer",
                    dependencies=("jd_analysis",),
                )
            ensure_task(
                state,
                task_type="rag_retrieval",
                assignee="rag_retriever",
                dependencies=("jd_analysis",),
            )
            planning_dependencies = ["jd_analysis"]
            if has_resume:
                planning_dependencies.append("resume_analysis")
            planning_dependencies.append("rag_retrieval")
            ensure_task(
                state,
                task_type="question_planning",
                assignee="question_planner",
                dependencies=tuple(planning_dependencies),
            )
            ensure_task(
                state,
                task_type="question_review",
                assignee="question_reviewer",
                dependencies=("question_planning",),
            )
            board = _board(state)
            completed = {
                task.get("task_type")
                for task in board["tasks"]
                if task.get("status") == "completed"
            }
            span.set_attribute("collaboration.task_count", len(board["tasks"]))
            span.set_attribute("collaboration.completed_count", len(completed))
            span.set_status(Status(StatusCode.OK))

    def finalize(self, state: MutableMapping[str, Any]) -> None:
        board = _board(state)
        review = next(
            (
                item for item in reversed(board["artifacts"])
                if item.get("artifact_type") == "question_review"
            ),
            None,
        )
        incomplete = [
            str(task.get("task_type"))
            for task in board["tasks"]
            if task.get("status") != "completed"
        ]
        status = "adopted"
        if not review:
            status = "degraded"
        elif str((review.get("content") or {}).get("decision") or "") == "revise":
            status = "rejected"
        elif str((review.get("content") or {}).get("decision") or "") == "accepted_with_warnings":
            status = "adopted_with_degradation"
        elif incomplete:
            status = "adopted_with_degradation"
        board["decision"] = {
            "status": status,
            "review_artifact_id": review.get("artifact_id") if review else None,
            "incomplete_tasks": incomplete,
            "at": _now(),
        }
        state["collaboration"] = board


class QuestionReviewerAgent:
    """Rule-based reviewer for relevance, completeness and duplicate questions."""

    name = "question_reviewer"

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        return SequenceMatcher(None, left.strip().lower(), right.strip().lower()).ratio()

    def run(self, state: MutableMapping[str, Any]) -> Dict[str, Any]:
        candidates = [dict(item) for item in state.get("question_candidates") or []]
        jd = state.get("jd_parsed") or {}
        terms = {
            str(item).strip().lower()
            for item in (
                [state.get("interview_topic", "")]
                + list(jd.get("tech_stack") or [])
                + list(jd.get("core_competencies") or [])
            )
            if str(item).strip()
        }
        approved: List[Dict[str, Any]] = []
        reviews: List[Dict[str, Any]] = []
        seen_prompts: List[str] = []
        context_diagnostics = state.get("context_diagnostics") or {}
        selected_evidence = {
            str(item)
            for item in context_diagnostics.get("selected_rag_source_ids") or []
            if str(item)
        }
        # Legacy states may not contain a context manifest. New runs only bind
        # evidence that was actually visible to the planning model.
        evidence_scope = (
            selected_evidence
            if "selected_rag_source_ids" in context_diagnostics
            else {
                str(item.get("id"))
                for item in state.get("rag_sources") or []
                if item.get("id")
            }
        )
        source_count = len(evidence_scope)

        tracer = get_tracer()
        with tracer.start_as_current_span("agent.collaboration.review") as span:
            for question in candidates:
                prompt = str(question.get("prompt") or "").strip()
                knowledge_point = str(question.get("knowledge_point") or "").strip()
                reasons: List[str] = []
                status = "approved"
                question_terms = [
                    str(question.get(key) or "").strip().lower()
                    for key in ("knowledge_point", "topic")
                    if str(question.get(key) or "").strip()
                ]
                evidence_ids = []
                for source in state.get("rag_sources") or []:
                    source_id = str(source.get("id") or "")
                    if source_id not in evidence_scope:
                        continue
                    searchable_source = " ".join((
                        str(source.get("text") or ""),
                        str(source.get("metadata") or ""),
                    )).lower()
                    if any(term in searchable_source for term in question_terms):
                        if source_id and source_id not in evidence_ids:
                            evidence_ids.append(source_id)
                    if len(evidence_ids) >= 3:
                        break
                if not prompt or not knowledge_point:
                    status = "rejected"
                    reasons.append("missing_required_content")
                elif any(self._similarity(prompt, previous) >= 0.92 for previous in seen_prompts):
                    status = "rejected"
                    reasons.append("semantic_duplicate")
                else:
                    searchable = " ".join(
                        str(question.get(key) or "").lower()
                        for key in ("topic", "knowledge_point", "focus", "prompt")
                    )
                    relevant = bool(question.get("is_anchor")) or not terms or any(
                        term in searchable or searchable in term for term in terms
                    )
                    if not relevant:
                        status = "approved_with_warning"
                        reasons.append("weak_job_relevance")
                    if not question.get("expected_points"):
                        status = "approved_with_warning"
                        reasons.append("missing_expected_points")
                    if not question.get("is_anchor") and source_count == 0:
                        reasons.append("rag_evidence_unavailable")

                review = {
                    "question_id": str(question.get("id") or ""),
                    "status": status,
                    "reasons": reasons,
                    "evidence_source_count": source_count,
                    "evidence_ids": evidence_ids,
                }
                reviews.append(review)
                question["review_status"] = status
                question["review_reasons"] = reasons
                question["evidence_ids"] = evidence_ids
                question["grounded"] = bool(evidence_ids) or bool(question.get("is_anchor"))
                if status != "rejected":
                    approved.append(question)
                    seen_prompts.append(prompt)

            budget = int(state.get("total_questions", 5))
            applied = len(approved) >= budget
            if applied:
                initialize_candidate_pool(state, approved)
                append_next_question(state)
                state["current_question_idx"] = 0
            else:
                # Availability wins when the LLM supplied too few valid alternatives.
                # The audit record still exposes the failed review.
                for question, review in zip(candidates, reviews):
                    question["review_status"] = review["status"]
                    question["review_reasons"] = review["reasons"]
                    question["evidence_ids"] = list(review["evidence_ids"])
                    question["grounded"] = bool(review["evidence_ids"]) or bool(
                        question.get("is_anchor")
                    )
                state["question_candidates"] = candidates

            summary = {
                "candidate_count": len(candidates),
                "approved_count": len(approved),
                "rejected_count": sum(1 for item in reviews if item["status"] == "rejected"),
                "warning_count": sum(
                    1 for item in reviews if item["status"] == "approved_with_warning"
                ),
                "filter_applied": applied,
                "reviews": reviews,
            }
            weak_relevance_count = sum(
                1
                for item in reviews
                if "weak_job_relevance" in item.get("reasons", [])
            )
            grounded_candidates = [
                item for item in approved if not item.get("is_anchor")
            ]
            grounded_count = sum(
                1 for item in grounded_candidates if item.get("grounded")
            )
            grounded_ratio = (
                grounded_count / len(grounded_candidates)
                if grounded_candidates else 1.0
            )
            decision_reasons: List[str] = []
            if not applied:
                decision_reasons.append("insufficient_approved_candidates")
            if weak_relevance_count > max(1, budget // 2):
                decision_reasons.append("excessive_weak_job_relevance")
            if decision_reasons:
                decision = "revise"
            elif summary["warning_count"] or grounded_ratio < 0.5:
                decision = "accepted_with_warnings"
            else:
                decision = "accepted"
            summary.update({
                "decision": decision,
                "requires_replan": decision == "revise",
                "decision_reasons": decision_reasons,
                "grounded_count": grounded_count,
                "grounded_ratio": round(grounded_ratio, 6),
            })
            state["question_review"] = summary
            submit_artifact(
                state,
                task_type="question_review",
                agent_name=self.name,
                artifact_type="question_review",
                content=summary,
                confidence=1.0,
                evidence=[str(item.get("id")) for item in state.get("rag_sources") or []],
            )
            span.set_attribute("collaboration.review.candidate_count", len(candidates))
            span.set_attribute("collaboration.review.approved_count", len(approved))
            span.set_attribute("collaboration.review.rejected_count", summary["rejected_count"])
            span.set_attribute("collaboration.review.filter_applied", applied)
            span.set_status(Status(StatusCode.OK))
        return dict(state)


interview_coordinator = InterviewCoordinatorAgent()
question_reviewer = QuestionReviewerAgent()
