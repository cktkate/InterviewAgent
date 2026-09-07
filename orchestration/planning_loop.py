"""Bounded Plan -> Tool -> Review primitives for interview question planning."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, Mapping, MutableMapping

from config import settings
from rag.metadata_filter import RetrievalMetadataFilter
from orchestration.security_policy import build_tool_context


ALLOWED_PLANNING_ACTIONS = frozenset({"rag_retrieve"})


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _merge_sources(
    existing: Iterable[Mapping[str, Any]],
    additional: Iterable[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    merged: list[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in [*existing, *additional]:
        item = dict(source)
        key = (str(item.get("id") or ""), _digest(str(item.get("text") or "")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def execute_evidence_requests(
    state: MutableMapping[str, Any],
    requests: Iterable[Mapping[str, Any]],
    *,
    max_actions: int | None = None,
    executor: Any = None,
) -> int:
    """Execute LLM-proposed actions through a deterministic allowlist and policy runtime.

    The LLM controls only the search intent. Identity, metadata isolation, limits,
    permissions and idempotency are injected by trusted orchestration code.
    """
    if executor is None:
        from tool_runtime import tool_executor

        executor = tool_executor
    limit = max(0, int(
        settings.agent_planning_max_evidence_actions
        if max_actions is None else max_actions
    ))
    actions = state.setdefault("planning_actions", [])
    attempted = {
        (str(item.get("action") or ""), str(item.get("query_digest") or ""))
        for item in actions
    }
    metadata_filter = RetrievalMetadataFilter.from_interview_state(state)
    executed = 0
    successful = 0

    for request in requests:
        if executed >= limit:
            break
        action = str(request.get("action") or "")
        query = " ".join(str(request.get("query") or "").split())
        query_digest = _digest(query)
        audit: Dict[str, Any] = {
            "attempt": int(state.get("planning_attempt", 1)),
            "action": action,
            "query_digest": query_digest,
            "query_chars": len(query),
            "reason": str(request.get("reason") or "")[:300],
            "required": bool(request.get("required", False)),
        }
        if action not in ALLOWED_PLANNING_ACTIONS:
            audit.update({"status": "rejected", "error_code": "action_not_allowed"})
            actions.append(audit)
            continue
        if not query:
            audit.update({"status": "rejected", "error_code": "empty_query"})
            actions.append(audit)
            continue
        if (action, query_digest) in attempted:
            audit.update({"status": "skipped", "error_code": "duplicate_action"})
            actions.append(audit)
            continue

        attempted.add((action, query_digest))
        executed += 1
        result = executor.execute(
            "rag.retrieve_interview_evidence",
            {
                "query": query,
                "top_k": min(10, max(3, int(settings.rag_top_k))),
                # Never trust identity or isolation fields from the model.
                "requester_id": metadata_filter.requester_id,
                "job_domains": list(metadata_filter.job_domains),
                "knowledge_points": list(metadata_filter.knowledge_points),
            },
            context=build_tool_context(
                state,
                agent_role="question_planner",
                tool_name="rag.retrieve_interview_evidence",
            ),
            idempotency_key=(
                f"planning:{state.get('session_id', '')}:{query_digest}"
            ),
        )
        summary = result.summary()
        audit.update(summary)
        actions.append(audit)
        state.setdefault("tool_decisions", []).append({
            **summary,
            "decision_source": "question_planner",
            "query_digest": query_digest,
        })
        if result.ok:
            new_sources = list(result.data or [])
            state["rag_sources"] = _merge_sources(
                state.get("rag_sources") or [], new_sources
            )
            state["rag_context"] = "\n---\n".join(
                str(item.get("text") or "")
                for item in state["rag_sources"]
                if item.get("text")
            )
            successful += 1

    state["planning_actions"] = actions[-20:]
    return successful


def review_feedback(review: Mapping[str, Any]) -> list[str]:
    """Convert deterministic review findings into bounded replanning guidance."""
    feedback = [str(item) for item in review.get("decision_reasons") or []]
    for item in review.get("reviews") or []:
        if item.get("status") in {"rejected", "approved_with_warning"}:
            question_id = str(item.get("question_id") or "unknown")
            for reason in item.get("reasons") or []:
                feedback.append(f"{question_id}:{reason}")
    return list(dict.fromkeys(feedback))[:12]


def record_planning_attempt(
    state: MutableMapping[str, Any],
    *,
    review: Mapping[str, Any] | None,
    evidence_request_count: int,
    evidence_action_successes: int,
) -> None:
    history = state.setdefault("planning_history", [])
    history.append({
        "attempt": int(state.get("planning_attempt", 1)),
        "candidate_count": len(state.get("question_candidates") or []),
        "evidence_request_count": int(evidence_request_count),
        "evidence_action_successes": int(evidence_action_successes),
        "review_decision": str((review or {}).get("decision") or "pending"),
        "approved_count": int((review or {}).get("approved_count") or 0),
        "rejected_count": int((review or {}).get("rejected_count") or 0),
        "warning_count": int((review or {}).get("warning_count") or 0),
    })
    state["planning_history"] = history[-10:]
