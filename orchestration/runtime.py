"""Runtime harness around the compiled LangGraph interview workflow."""
from __future__ import annotations

import asyncio
import time
import uuid
from functools import wraps
from typing import Any, AsyncIterator, Callable, Dict, Mapping, MutableMapping, Optional, Tuple

from loguru import logger
from opentelemetry.trace import Status, StatusCode

from config import settings
from observability.tracing import configure_tracing, get_tracer
from observability.metrics import record_agent_run
from orchestration.budget import AgentBudgetExceeded, RuntimeBudget, runtime_budget


RUNTIME_RUN_ID = "runtime_run_id"
RUNTIME_TRACE_ID = "runtime_trace_id"


def _configure_tracing() -> None:
    configure_tracing(
        enabled=settings.observability_enabled,
        service_name=settings.otel_service_name,
        jsonl_enabled=settings.trace_jsonl_enabled,
        jsonl_path=settings.trace_jsonl_path,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
    )


def _set_safe_attributes(span, state: Mapping[str, Any], prefix: str) -> None:
    """Attach bounded operational metadata, never user-provided text."""
    rag_diagnostics = state.get("rag_diagnostics") or {}
    safe_values = {
        "session_id": state.get("session_id"),
        "run_id": state.get(RUNTIME_RUN_ID),
        "intent": state.get("intent"),
        "question_index": state.get("current_question_idx"),
        "difficulty": state.get("current_difficulty"),
        "qa_count": len(state.get("qa_history") or []),
        "rag_source_count": len(state.get("rag_sources") or []),
        "rag_vector_status": (rag_diagnostics.get("vector") or {}).get("status"),
        "rag_vector_count": (rag_diagnostics.get("vector") or {}).get("count"),
        "rag_bm25_status": (rag_diagnostics.get("bm25") or {}).get("status"),
        "rag_bm25_count": (rag_diagnostics.get("bm25") or {}).get("count"),
        "rag_rerank_status": (rag_diagnostics.get("rerank") or {}).get("status"),
        "completed_primary_questions": state.get("completed_primary_questions"),
        "candidate_count": len(state.get("question_candidates") or []),
        "anchor_count": state.get("anchor_count"),
        "ability_skill_count": len(state.get("ability_profile") or {}),
        "policy_decision_count": len(state.get("policy_decisions") or []),
        "tool_decision_count": len(state.get("tool_decisions") or []),
        "collaboration_task_count": len(
            (state.get("collaboration") or {}).get("tasks") or []
        ),
        "collaboration_artifact_count": len(
            (state.get("collaboration") or {}).get("artifacts") or []
        ),
        "question_review_rejected_count": (
            state.get("question_review") or {}
        ).get("rejected_count"),
        "question_grounded_count": sum(
            1 for item in state.get("question_candidates") or [] if item.get("grounded")
        ),
        "collaboration_status": (
            state.get("collaboration") or {}
        ).get("decision", {}).get("status"),
        "runtime_llm_calls": (state.get("runtime_usage") or {}).get("llm_calls"),
        "runtime_tool_calls": (state.get("runtime_usage") or {}).get("tool_calls"),
        "runtime_total_tokens": (state.get("runtime_usage") or {}).get("total_tokens"),
        "runtime_structured_retries": (
            state.get("runtime_usage") or {}
        ).get("structured_retries"),
        "runtime_budget_exceeded": (
            state.get("runtime_usage") or {}
        ).get("budget_exceeded"),
        "runtime_budget_dimension": (
            state.get("runtime_usage") or {}
        ).get("budget_dimension"),
        "planning_fallback": state.get("planning_fallback"),
        "planning_attempt": state.get("planning_attempt"),
        "planning_action_count": len(state.get("planning_actions") or []),
        "planning_decision": state.get("planning_decision"),
        "security_event_count": len(state.get("security_events") or []),
        "context_security_signal_count": (
            state.get("context_diagnostics") or {}
        ).get("security_signal_count"),
        "context_selected_chars": (
            state.get("context_diagnostics") or {}
        ).get("selected_chars"),
        "context_selected_tokens": (
            state.get("context_diagnostics") or {}
        ).get("selected_tokens"),
        "context_max_tokens": (
            state.get("context_diagnostics") or {}
        ).get("max_tokens"),
        "context_token_utilization": (
            state.get("context_diagnostics") or {}
        ).get("token_utilization"),
        "context_selected_rag_source_count": (
            state.get("context_diagnostics") or {}
        ).get("selected_rag_source_count"),
        "context_compressed_source_count": len(
            (state.get("context_diagnostics") or {}).get("compressed_sources") or []
        ),
        "context_dropped_source_count": len(
            (state.get("context_diagnostics") or {}).get("dropped_sources") or []
        ),
        "context_tokenizer": (
            state.get("context_diagnostics") or {}
        ).get("tokenizer"),
        "rag_query_rewrite_status": (
            (state.get("rag_diagnostics") or {}).get("query_rewrite") or {}
        ).get("status"),
        "rag_metadata_filter_level": (
            (state.get("rag_diagnostics") or {}).get("metadata_filter") or {}
        ).get("level"),
        "followup_type": state.get("current_followup_type"),
        "calibrated_overall_score": (state.get("calibrated_scoring") or {}).get(
            "overall_score"
        ),
        "interview_finished": state.get("interview_finished"),
    }
    for key, value in safe_values.items():
        if isinstance(value, (str, bool, int, float)):
            span.set_attribute(f"{prefix}.{key}", value)


def trace_node(name: str, node: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
    """Wrap a LangGraph node in an OpenTelemetry child span."""

    @wraps(node)
    def wrapped(state: Dict[str, Any], *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if not settings.observability_enabled:
            return node(state, *args, **kwargs)

        _configure_tracing()
        tracer = get_tracer()
        started = time.perf_counter()
        with tracer.start_as_current_span(f"agent.node.{name}") as span:
            span.set_attribute("agent.node.name", name)
            _set_safe_attributes(span, state, "agent.input")
            try:
                result = node(state, *args, **kwargs)
                if isinstance(result, Mapping):
                    _set_safe_attributes(span, result, "agent.output")
                span.set_attribute("agent.node.status", "success")
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as exc:
                span.set_attribute("agent.node.status", "failed")
                span.set_attribute("error.type", type(exc).__name__)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)[:300]))
                raise
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000
                span.set_attribute("agent.node.duration_ms", elapsed_ms)
                logger.bind(
                    run_id=state.get(RUNTIME_RUN_ID),
                    node=name,
                ).debug("Agent node completed in {:.1f}ms", elapsed_ms)

    return wrapped


class AgentRuntimeGraph:
    """Preserve the compiled graph API while adding run lifecycle tracing."""

    def __init__(
        self,
        compiled_graph: Any,
        *,
        checkpoint_enabled: bool = False,
        checkpoint_backend: str = "disabled",
    ) -> None:
        self._compiled_graph = compiled_graph
        self._checkpoint_enabled = checkpoint_enabled
        self.checkpoint_backend = checkpoint_backend
        _configure_tracing()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._compiled_graph, name)

    @staticmethod
    def _checkpoint_config(session_id: str) -> Dict[str, Any]:
        """Use the business session id as the stable LangGraph thread id."""
        return {"configurable": {"thread_id": str(session_id)}}

    def load_checkpoint_state(self, session_id: str) -> Dict[str, Any]:
        """Load the latest durable LangGraph state for one interview session."""
        if not self._checkpoint_enabled or not session_id:
            return {}
        snapshot = self._compiled_graph.get_state(
            self._checkpoint_config(session_id)
        )
        return dict(snapshot.values or {}) if snapshot else {}

    def save_checkpoint_state(
        self,
        session_id: str,
        state: Mapping[str, Any],
    ) -> bool:
        """Persist state changed outside graph nodes into the graph thread."""
        if not self._checkpoint_enabled or not session_id or not state:
            return False
        self._compiled_graph.update_state(
            self._checkpoint_config(session_id),
            dict(state),
        )
        return True

    @staticmethod
    def _prepare_run(
        input_state: Mapping[str, Any],
        config: Optional[MutableMapping[str, Any]],
    ) -> Tuple[str, str, Dict[str, Any], Dict[str, Any]]:
        """Build one consistent runtime/checkpoint context for invoke and astream."""
        run_id = str(uuid.uuid4())
        state = dict(input_state)
        session_id = str(state.get("session_id") or run_id)
        state[RUNTIME_RUN_ID] = run_id

        effective_config: Dict[str, Any] = dict(config or {})
        configurable = dict(effective_config.get("configurable") or {})
        configurable.setdefault("thread_id", session_id)
        effective_config["configurable"] = configurable
        metadata = dict(effective_config.get("metadata") or {})
        metadata.update({"run_id": run_id, "session_id": session_id})
        effective_config["metadata"] = metadata
        effective_config.setdefault("recursion_limit", settings.agent_max_steps)
        return run_id, session_id, state, effective_config

    @staticmethod
    def _runtime_budget() -> RuntimeBudget:
        return RuntimeBudget(
            max_llm_calls=max(1, settings.agent_max_llm_calls),
            max_total_tokens=max(1, settings.agent_max_total_tokens),
            deadline_seconds=max(1.0, settings.agent_deadline_seconds),
            max_tool_calls=max(1, settings.agent_max_tool_calls),
        )

    def invoke(
        self,
        input_state: Mapping[str, Any],
        config: Optional[MutableMapping[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        run_id, session_id, state, effective_config = self._prepare_run(
            input_state, config
        )

        if not settings.observability_enabled:
            started = time.perf_counter()
            outcome = "failure"
            try:
                with runtime_budget(self._runtime_budget()) as usage:
                    result = self._compiled_graph.invoke(state, effective_config, **kwargs)
                    if isinstance(result, dict):
                        result["runtime_usage"] = usage.snapshot()
                    outcome = "success"
                    return result
            finally:
                record_agent_run(
                    str(state.get("intent") or "interview"),
                    outcome,
                    time.perf_counter() - started,
                )

        tracer = get_tracer()
        started = time.perf_counter()
        operation = str(state.get("intent") or "interview")
        outcome = "failure"
        with tracer.start_as_current_span(f"agent.run.{operation}") as span:
            span_context = span.get_span_context()
            trace_id = format(span_context.trace_id, "032x")
            state[RUNTIME_TRACE_ID] = trace_id
            span.set_attribute("agent.run.id", run_id)
            span.set_attribute("agent.session.id", session_id)
            span.set_attribute("agent.run.operation", operation)
            span.set_attribute("agent.run.checkpoint_backend", self.checkpoint_backend)
            _set_safe_attributes(span, state, "agent.run.input")
            latest_state: Dict[str, Any] = {}
            try:
                with runtime_budget(self._runtime_budget()) as usage:
                    result = self._compiled_graph.invoke(state, effective_config, **kwargs)
                    if isinstance(result, dict):
                        result["runtime_usage"] = usage.snapshot()
                if isinstance(result, dict):
                    result[RUNTIME_RUN_ID] = run_id
                    result[RUNTIME_TRACE_ID] = trace_id
                    _set_safe_attributes(span, result, "agent.run.output")
                span.set_attribute("agent.run.status", "success")
                span.set_status(Status(StatusCode.OK))
                outcome = "success"
                return result
            except Exception as exc:
                span.set_attribute("agent.run.status", "failed")
                span.set_attribute("error.type", type(exc).__name__)
                if isinstance(exc, AgentBudgetExceeded):
                    span.set_attribute("agent.run.budget_exceeded", True)
                    span.set_attribute("agent.run.budget_dimension", exc.dimension)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)[:300]))
                raise
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000
                record_agent_run(operation, outcome, elapsed_ms / 1000.0)
                span.set_attribute("agent.run.duration_ms", elapsed_ms)
                logger.bind(run_id=run_id, trace_id=trace_id).info(
                    "Agent run completed: session={} operation={} duration={:.1f}ms",
                    session_id,
                    operation,
                    elapsed_ms,
                )

    async def _stream_compiled(
        self,
        state: Dict[str, Any],
        effective_config: Dict[str, Any],
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Bridge the sync SqliteSaver graph stream without blocking asyncio."""
        loop = asyncio.get_running_loop()
        updates: asyncio.Queue[Tuple[str, Any]] = asyncio.Queue()

        def produce() -> None:
            try:
                for chunk in self._compiled_graph.stream(
                    state, effective_config, **kwargs
                ):
                    loop.call_soon_threadsafe(updates.put_nowait, ("data", chunk))
            except BaseException as exc:
                loop.call_soon_threadsafe(updates.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(updates.put_nowait, ("end", None))

        producer = asyncio.create_task(asyncio.to_thread(produce))
        try:
            while True:
                kind, payload = await updates.get()
                if kind == "data":
                    yield payload
                elif kind == "error":
                    raise payload
                else:
                    break
        finally:
            await producer

    async def astream(
        self,
        input_state: Mapping[str, Any],
        config: Optional[MutableMapping[str, Any]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Stream graph updates while preserving run IDs, checkpoints and tracing."""
        run_id, session_id, state, effective_config = self._prepare_run(
            input_state, config
        )

        if not settings.observability_enabled:
            started = time.perf_counter()
            outcome = "failure"
            try:
                with runtime_budget(self._runtime_budget()):
                    async for chunk in self._stream_compiled(
                        state, effective_config, **kwargs
                    ):
                        yield chunk
                outcome = "success"
            finally:
                record_agent_run(
                    str(state.get("intent") or "interview"),
                    outcome,
                    time.perf_counter() - started,
                )
            return

        tracer = get_tracer()
        started = time.perf_counter()
        operation = str(state.get("intent") or "interview")
        outcome = "failure"
        with tracer.start_as_current_span(f"agent.run.{operation}") as span:
            span_context = span.get_span_context()
            trace_id = format(span_context.trace_id, "032x")
            state[RUNTIME_TRACE_ID] = trace_id
            span.set_attribute("agent.run.id", run_id)
            span.set_attribute("agent.session.id", session_id)
            span.set_attribute("agent.run.operation", operation)
            span.set_attribute("agent.run.transport", "astream")
            span.set_attribute("agent.run.checkpoint_backend", self.checkpoint_backend)
            _set_safe_attributes(span, state, "agent.run.input")
            try:
                with runtime_budget(self._runtime_budget()) as usage:
                    async for chunk in self._stream_compiled(
                        state, effective_config, **kwargs
                    ):
                        if isinstance(chunk, dict):
                            for value in chunk.values():
                                if isinstance(value, dict):
                                    value["runtime_usage"] = usage.snapshot()
                                    latest_state.update(value)
                        yield chunk
                    usage_snapshot = usage.snapshot()
                    latest_state["runtime_usage"] = usage_snapshot
                    _set_safe_attributes(span, latest_state, "agent.run.output")
                span.set_attribute("agent.run.status", "success")
                span.set_status(Status(StatusCode.OK))
                outcome = "success"
            except Exception as exc:
                span.set_attribute("agent.run.status", "failed")
                span.set_attribute("error.type", type(exc).__name__)
                if isinstance(exc, AgentBudgetExceeded):
                    span.set_attribute("agent.run.budget_exceeded", True)
                    span.set_attribute("agent.run.budget_dimension", exc.dimension)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)[:300]))
                raise
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000
                record_agent_run(operation, outcome, elapsed_ms / 1000.0)
                span.set_attribute("agent.run.duration_ms", elapsed_ms)
                logger.bind(run_id=run_id, trace_id=trace_id).info(
                    "Agent stream completed: session={} operation={} duration={:.1f}ms",
                    session_id,
                    operation,
                    elapsed_ms,
                )
