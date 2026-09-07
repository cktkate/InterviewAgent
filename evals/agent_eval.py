from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.common import mean, percentile, read_jsonl


class AgentTraceCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=120)
    trace_id: str = Field(default="", max_length=64)
    session_id: str = Field(default="", max_length=128)
    operation: str = Field(default="", max_length=120)
    expected_nodes: List[str] = Field(default_factory=list)
    required_nodes: List[str] = Field(default_factory=list)
    forbidden_nodes: List[str] = Field(default_factory=list)
    expected_terminal: str = ""
    expected_tools: List[str] = Field(default_factory=list)
    max_node_visits: Dict[str, int] = Field(default_factory=dict)
    default_max_node_visits: int = Field(default=2, ge=1, le=100)
    max_duration_ms: float | None = Field(default=None, gt=0)
    max_llm_calls: int | None = Field(default=None, ge=0, le=1000)
    max_tool_calls: int | None = Field(default=None, ge=0, le=1000)
    max_total_tokens: int | None = Field(default=None, ge=0)
    max_planning_attempts: int | None = Field(default=None, ge=1, le=20)
    allowed_planning_decisions: List[str] = Field(default_factory=list)
    require_collaboration_review: bool = False
    tags: List[str] = Field(default_factory=list)
    bad_case_reasons: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case(self):
        if not (self.trace_id or self.session_id or self.operation):
            raise ValueError("trace_id, session_id or operation is required")
        if not (
            self.expected_nodes
            or self.required_nodes
            or self.forbidden_nodes
            or self.expected_terminal
            or self.expected_tools
            or self.max_duration_ms is not None
            or self.max_llm_calls is not None
            or self.max_tool_calls is not None
            or self.max_total_tokens is not None
            or self.max_planning_attempts is not None
            or self.allowed_planning_decisions
            or self.require_collaboration_review
        ):
            raise ValueError("at least one trajectory expectation is required")
        return self


def load_agent_cases(path: str) -> List[AgentTraceCase]:
    return [AgentTraceCase.model_validate(row) for row in read_jsonl(path)]


def load_trace_groups(path: str) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(path):
        trace_id = str(row.get("trace_id") or "")
        if trace_id:
            groups[trace_id].append(row)
    for spans in groups.values():
        spans.sort(key=lambda item: int(item.get("start_time_unix_nano") or 0))
    return dict(groups)


def _root_span(spans: Iterable[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    roots = [item for item in spans if str(item.get("name") or "").startswith("agent.run.")]
    return roots[-1] if roots else None


def _select_trace(
    case: AgentTraceCase,
    groups: Mapping[str, List[Dict[str, Any]]],
) -> tuple[str, List[Dict[str, Any]]]:
    if case.trace_id:
        return case.trace_id, list(groups.get(case.trace_id) or [])
    candidates = []
    for trace_id, spans in groups.items():
        root = _root_span(spans)
        if root is None:
            continue
        attributes = dict(root.get("attributes") or {})
        if case.session_id and str(attributes.get("agent.session.id") or "") != case.session_id:
            continue
        if case.operation and str(attributes.get("agent.run.operation") or "") != case.operation:
            continue
        candidates.append((int(root.get("start_time_unix_nano") or 0), trace_id, spans))
    if not candidates:
        return "", []
    _, trace_id, spans = max(candidates, key=lambda item: item[0])
    return trace_id, list(spans)


def _node_name(span: Mapping[str, Any]) -> str:
    attributes = dict(span.get("attributes") or {})
    return str(attributes.get("agent.node.name") or str(span.get("name") or "").removeprefix("agent.node."))


def _tool_name(span: Mapping[str, Any]) -> str:
    attributes = dict(span.get("attributes") or {})
    return str(attributes.get("agent.tool.name") or str(span.get("name") or "").removeprefix("agent.tool."))


def evaluate_agent_case(
    case: AgentTraceCase,
    groups: Mapping[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    trace_id, spans = _select_trace(case, groups)
    root = _root_span(spans)
    if not spans or root is None:
        return {
            "case_id": case.case_id,
            "trace_id": trace_id,
            "matched": False,
            "passed": False,
            "status": "trace_not_found",
            "nodes": [],
            "tools": [],
        }

    node_spans = [item for item in spans if str(item.get("name") or "").startswith("agent.node.")]
    tool_spans = [item for item in spans if str(item.get("name") or "").startswith("agent.tool.")]
    nodes = [_node_name(item) for item in node_spans]
    tools = [_tool_name(item) for item in tool_spans]
    counts = Counter(nodes)

    exact_match = nodes == case.expected_nodes if case.expected_nodes else None
    required = list(dict.fromkeys(case.required_nodes))
    required_coverage = (
        sum(1 for name in required if name in counts) / len(required)
        if required else 1.0
    )
    forbidden_hits = [name for name in case.forbidden_nodes if name in counts]
    terminal_match = nodes[-1] == case.expected_terminal if case.expected_terminal else None
    expected_tools = list(dict.fromkeys(case.expected_tools))
    tool_recall = (
        sum(1 for name in expected_tools if name in tools) / len(expected_tools)
        if expected_tools else 1.0
    )
    loop_violations = {
        name: visits
        for name, visits in counts.items()
        if visits > int(case.max_node_visits.get(name, case.default_max_node_visits))
    }
    duration_ms = float(root.get("duration_ms") or 0.0)
    duration_pass = case.max_duration_ms is None or duration_ms <= case.max_duration_ms
    root_status = str(root.get("status") or "unset")
    tool_successes = sum(1 for item in tool_spans if str(item.get("status") or "") == "ok")
    tool_success_rate = tool_successes / len(tool_spans) if tool_spans else 1.0
    root_attributes = dict(root.get("attributes") or {})
    llm_calls = int(root_attributes.get("agent.run.output.runtime_llm_calls") or 0)
    total_tokens = int(root_attributes.get("agent.run.output.runtime_total_tokens") or 0)
    tool_calls = int(root_attributes.get("agent.run.output.runtime_tool_calls") or 0)
    planning_attempts = int(root_attributes.get("agent.run.output.planning_attempt") or 0)
    planning_action_count = int(
        root_attributes.get("agent.run.output.planning_action_count") or 0
    )
    planning_decision = str(
        root_attributes.get("agent.run.output.planning_decision") or ""
    )
    security_signal_count = int(
        root_attributes.get("agent.run.output.security_event_count") or 0
    ) + int(
        root_attributes.get("agent.run.output.context_security_signal_count") or 0
    )
    context_selected_tokens = int(
        root_attributes.get("agent.run.output.context_selected_tokens") or 0
    )
    context_max_tokens = int(
        root_attributes.get("agent.run.output.context_max_tokens") or 0
    )
    context_token_utilization = float(
        root_attributes.get("agent.run.output.context_token_utilization") or 0.0
    )
    context_selected_rag_source_count = int(
        root_attributes.get("agent.run.output.context_selected_rag_source_count") or 0
    )
    context_compressed_source_count = int(
        root_attributes.get("agent.run.output.context_compressed_source_count") or 0
    )
    context_dropped_source_count = int(
        root_attributes.get("agent.run.output.context_dropped_source_count") or 0
    )
    budget_exceeded = bool(
        root_attributes.get("agent.run.budget_exceeded", False)
        or root_attributes.get("agent.run.output.runtime_budget_exceeded", False)
    )
    collaboration_status = str(
        root_attributes.get("agent.run.output.collaboration_status") or ""
    )
    collaboration_reviewed = collaboration_status.startswith("adopted")
    llm_budget_pass = case.max_llm_calls is None or llm_calls <= case.max_llm_calls
    token_budget_pass = (
        case.max_total_tokens is None or total_tokens <= case.max_total_tokens
    )
    tool_budget_pass = (
        case.max_tool_calls is None or tool_calls <= case.max_tool_calls
    )
    planning_attempt_pass = (
        case.max_planning_attempts is None
        or planning_attempts <= case.max_planning_attempts
    )
    planning_decision_pass = (
        not case.allowed_planning_decisions
        or planning_decision in case.allowed_planning_decisions
    )
    collaboration_pass = (
        not case.require_collaboration_review or collaboration_reviewed
    )

    checks = [
        root_status == "ok",
        required_coverage == 1.0,
        not forbidden_hits,
        not loop_violations,
        tool_recall == 1.0,
        duration_pass,
        not budget_exceeded,
        llm_budget_pass,
        token_budget_pass,
        tool_budget_pass,
        planning_attempt_pass,
        planning_decision_pass,
        collaboration_pass,
    ]
    if exact_match is not None:
        checks.append(exact_match)
    if terminal_match is not None:
        checks.append(terminal_match)

    return {
        "case_id": case.case_id,
        "trace_id": trace_id,
        "matched": True,
        "passed": all(checks),
        "status": root_status,
        "operation": dict(root.get("attributes") or {}).get("agent.run.operation", ""),
        "nodes": nodes,
        "tools": tools,
        "exact_path_match": exact_match,
        "required_node_coverage": round(required_coverage, 6),
        "forbidden_hits": forbidden_hits,
        "terminal_match": terminal_match,
        "expected_tool_recall": round(tool_recall, 6),
        "tool_success_rate": round(tool_success_rate, 6),
        "loop_violations": loop_violations,
        "duration_ms": round(duration_ms, 3),
        "duration_pass": duration_pass,
        "llm_calls": llm_calls,
        "total_tokens": total_tokens,
        "tool_calls": tool_calls,
        "budget_exceeded": budget_exceeded,
        "llm_budget_pass": llm_budget_pass,
        "token_budget_pass": token_budget_pass,
        "tool_budget_pass": tool_budget_pass,
        "planning_attempts": planning_attempts,
        "planning_action_count": planning_action_count,
        "planning_decision": planning_decision,
        "planning_attempt_pass": planning_attempt_pass,
        "planning_decision_pass": planning_decision_pass,
        "collaboration_status": collaboration_status,
        "collaboration_reviewed": collaboration_reviewed,
        "security_signal_count": security_signal_count,
        "context_selected_tokens": context_selected_tokens,
        "context_max_tokens": context_max_tokens,
        "context_token_utilization": context_token_utilization,
        "context_selected_rag_source_count": context_selected_rag_source_count,
        "context_compressed_source_count": context_compressed_source_count,
        "context_dropped_source_count": context_dropped_source_count,
    }


def run_agent_evaluation(
    cases: Iterable[AgentTraceCase],
    trace_groups: Mapping[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    results = [evaluate_agent_case(case, trace_groups) for case in cases]
    matched = [item for item in results if item.get("matched")]
    exact_labeled = [item for item in matched if item.get("exact_path_match") is not None]
    terminal_labeled = [item for item in matched if item.get("terminal_match") is not None]
    durations = [float(item.get("duration_ms") or 0.0) for item in matched]
    llm_calls = [float(item.get("llm_calls") or 0.0) for item in matched]
    total_tokens = [float(item.get("total_tokens") or 0.0) for item in matched]
    context_tokens = [
        float(item.get("context_selected_tokens") or 0.0) for item in matched
    ]
    context_utilization = [
        float(item.get("context_token_utilization") or 0.0) for item in matched
    ]
    tool_calls = [float(item.get("tool_calls") or 0.0) for item in matched]
    planning_items = [
        item for item in matched if int(item.get("planning_attempts") or 0) > 0
    ]
    metrics = {
        "case_count": len(results),
        "trace_match_rate": mean(1.0 if item.get("matched") else 0.0 for item in results),
        "task_success_rate": mean(1.0 if item.get("passed") else 0.0 for item in results),
        "exact_path_rate": mean(
            1.0 if item.get("exact_path_match") else 0.0 for item in exact_labeled
        ),
        "required_node_coverage": mean(
            float(item.get("required_node_coverage") or 0.0) for item in matched
        ),
        "terminal_accuracy": mean(
            1.0 if item.get("terminal_match") else 0.0 for item in terminal_labeled
        ),
        "forbidden_violation_rate": mean(
            1.0 if item.get("forbidden_hits") else 0.0 for item in matched
        ),
        "loop_violation_rate": mean(
            1.0 if item.get("loop_violations") else 0.0 for item in matched
        ),
        "expected_tool_recall": mean(
            float(item.get("expected_tool_recall") or 0.0) for item in matched
        ),
        "tool_success_rate": mean(
            float(item.get("tool_success_rate") or 0.0) for item in matched
        ),
        "p50_latency_ms": percentile(durations, 0.50),
        "p95_latency_ms": percentile(durations, 0.95),
        "average_llm_calls": mean(llm_calls),
        "average_tool_calls": mean(tool_calls),
        "p95_total_tokens": percentile(total_tokens, 0.95),
        "average_context_tokens": mean(context_tokens),
        "p95_context_tokens": percentile(context_tokens, 0.95),
        "average_context_token_utilization": mean(context_utilization),
        "average_context_selected_rag_sources": mean(
            float(item.get("context_selected_rag_source_count") or 0.0)
            for item in matched
        ),
        "average_context_compressed_sources": mean(
            float(item.get("context_compressed_source_count") or 0.0)
            for item in matched
        ),
        "average_context_dropped_sources": mean(
            float(item.get("context_dropped_source_count") or 0.0)
            for item in matched
        ),
        "budget_violation_rate": mean(
            1.0 if item.get("budget_exceeded") else 0.0 for item in matched
        ),
        "collaboration_review_rate": mean(
            1.0 if item.get("collaboration_reviewed") else 0.0 for item in matched
        ),
        "planning_replan_rate": mean(
            1.0 if int(item.get("planning_attempts") or 0) > 1 else 0.0
            for item in planning_items
        ),
        "planning_evidence_action_rate": mean(
            1.0 if int(item.get("planning_action_count") or 0) > 0 else 0.0
            for item in planning_items
        ),
        "planning_recovery_rate": mean(
            1.0 if item.get("planning_decision") == "deterministic_recovery" else 0.0
            for item in planning_items
        ),
        "security_signal_rate": mean(
            1.0 if int(item.get("security_signal_count") or 0) > 0 else 0.0
            for item in matched
        ),
    }
    return {
        "evaluation": "agent_trajectory",
        "metrics": {key: round(value, 6) if isinstance(value, float) else value for key, value in metrics.items()},
        "cases": results,
    }


def build_case_template(trace_id: str, spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    root = _root_span(spans)
    if root is None:
        raise ValueError("trace does not contain an agent.run span")
    nodes = [
        _node_name(item) for item in spans
        if str(item.get("name") or "").startswith("agent.node.")
    ]
    tools = [
        _tool_name(item) for item in spans
        if str(item.get("name") or "").startswith("agent.tool.")
    ]
    attributes = dict(root.get("attributes") or {})
    return {
        "case_id": f"review-{trace_id[:8]}",
        "trace_id": trace_id,
        "session_id": str(attributes.get("agent.session.id") or ""),
        "operation": str(attributes.get("agent.run.operation") or ""),
        "expected_nodes": nodes,
        "required_nodes": list(dict.fromkeys(nodes)),
        "forbidden_nodes": [],
        "expected_terminal": nodes[-1] if nodes else "",
        "expected_tools": list(dict.fromkeys(tools)),
        "max_node_visits": dict(Counter(nodes)),
        "default_max_node_visits": 2,
        "max_duration_ms": round(float(root.get("duration_ms") or 0.0) * 1.5 + 1.0, 3),
        "max_llm_calls": None,
        "max_tool_calls": None,
        "max_total_tokens": None,
        "max_planning_attempts": None,
        "allowed_planning_decisions": [],
        "require_collaboration_review": False,
        "tags": ["needs-human-review"],
    }


def collect_bad_case_templates(
    groups: Mapping[str, List[Dict[str, Any]]],
    *,
    max_duration_ms: float = 30000.0,
    structured_retry_threshold: int = 1,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Convert operationally suspicious traces into human-reviewable cases."""
    candidates: List[tuple[int, str, List[Dict[str, Any]], List[str]]] = []
    for trace_id, spans in groups.items():
        root = _root_span(spans)
        if root is None:
            continue
        attributes = dict(root.get("attributes") or {})
        reasons: List[str] = []
        if str(root.get("status") or "unset") != "ok":
            reasons.append("run_failed")
        duration_ms = float(root.get("duration_ms") or 0.0)
        if duration_ms > max_duration_ms:
            reasons.append("latency_threshold_exceeded")
        if bool(attributes.get("agent.run.budget_exceeded", False)):
            reasons.append("runtime_budget_exceeded")
        retries = int(
            attributes.get("agent.run.output.runtime_structured_retries") or 0
        )
        if retries >= structured_retry_threshold:
            reasons.append("structured_output_retried")
        collaboration_status = str(
            attributes.get("agent.run.output.collaboration_status") or ""
        )
        if collaboration_status in {"degraded", "adopted_with_degradation"}:
            reasons.append("collaboration_degraded")
        if str(attributes.get("agent.run.output.planning_decision") or "") == (
            "deterministic_recovery"
        ):
            reasons.append("planning_recovery_used")
        if int(attributes.get("agent.run.output.security_event_count") or 0) > 0:
            reasons.append("untrusted_input_signal")
        if int(
            attributes.get("agent.run.output.context_security_signal_count") or 0
        ) > 0:
            reasons.append("untrusted_rag_signal")
        if any(str(span.get("status") or "") == "error" for span in spans):
            reasons.append("child_span_failed")
        if reasons:
            candidates.append((
                int(root.get("start_time_unix_nano") or 0),
                trace_id,
                spans,
                list(dict.fromkeys(reasons)),
            ))

    templates = []
    for _, trace_id, spans, reasons in sorted(candidates, reverse=True)[: max(1, limit)]:
        template = build_case_template(trace_id, spans)
        template["case_id"] = f"bad-{trace_id[:8]}"
        template["tags"] = ["bad-case", "needs-human-review", *reasons]
        template["bad_case_reasons"] = reasons
        templates.append(template)
    return templates
