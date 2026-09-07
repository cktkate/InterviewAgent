"""Celery tasks for asynchronous interview finalization."""
from __future__ import annotations

from typing import Any, Dict
import time

from loguru import logger
from opentelemetry.propagate import extract

from config import settings
from memory.memory_manager import memory_manager
from memory.task_repository import background_task_repository
from observability.tracing import configure_tracing, get_tracer
from observability.metrics import REPORT_RETRIES, record_report_task
from orchestration.security_policy import redact_sensitive_text
from tasks.celery_app import celery_app
from tasks.event_bus import publish_task_event


def _progress(
    state: Dict[str, Any],
    *,
    task_id: str,
    status: str,
    stage: str,
    progress: int,
    attempt: int,
    trace_id: str,
    message: str,
) -> None:
    state["report_status"] = status
    state["report_stage"] = stage
    state["report_progress"] = progress
    state["report_attempt"] = attempt
    memory_manager.save_session(str(state.get("session_id") or ""), state)
    background_task_repository.update(
        task_id,
        status=status,
        stage=stage,
        progress=progress,
        attempt=attempt,
        trace_id=trace_id,
    )
    publish_task_event(
        session_id=str(state.get("session_id") or ""),
        task_id=task_id,
        status=status,
        stage=stage,
        progress=progress,
        attempt=attempt,
        trace_id=trace_id,
        message=message,
    )


def _task_elapsed_seconds(task_id: str, fallback_started: float) -> float:
    """Prefer persisted queue-to-terminal latency; fall back to this attempt."""
    try:
        record = background_task_repository.get(task_id)
        created_at = record.get("created_at")
        if created_at is not None:
            from memory.time_utils import utc_now

            return max(0.0, (utc_now() - created_at).total_seconds())
    except Exception:
        # Metrics must never turn a completed business task into a failure.
        pass
    return max(0.0, time.perf_counter() - fallback_started)


@celery_app.task(
    bind=True,
    name="interview.generate_report",
    max_retries=3,
)
def generate_report_task(self, session_id: str) -> Dict[str, Any]:
    """Generate the final report outside the FastAPI/WebSocket process."""
    configure_tracing(
        enabled=settings.observability_enabled,
        service_name=settings.otel_service_name,
        jsonl_enabled=settings.trace_jsonl_enabled,
        jsonl_path=settings.trace_jsonl_path,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
    )
    headers = dict(getattr(self.request, "headers", None) or {})
    parent_context = extract(headers)
    task_id = str(getattr(self.request, "id", "") or "")
    attempt = int(self.request.retries or 0) + 1
    attempt_started = time.perf_counter()
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "agent.background.report", context=parent_context
    ) as span:
        span.set_attribute("agent.background.task_id", task_id)
        span.set_attribute("agent.background.session_id", session_id)
        span.set_attribute("agent.background.attempt", attempt)
        span_context = span.get_span_context()
        trace_id = (
            f"{span_context.trace_id:032x}"
            if span_context.is_valid
            else str(headers.get("x-agent-trace-id") or "")
        )
        state = memory_manager.load_session(session_id)
        if not state:
            exc = ValueError("Interview session state unavailable")
            can_retry = self.request.retries < self.max_retries
            status = "retrying" if can_retry else "failed"
            message = (
                "会话状态暂不可用，正在重试"
                if can_retry else "报告生成失败，请稍后重试"
            )
            background_task_repository.update(
                task_id,
                status=status,
                stage="load_state",
                progress=5 if can_retry else 100,
                attempt=attempt,
                trace_id=trace_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            publish_task_event(
                session_id=session_id,
                task_id=task_id,
                status=status,
                stage="load_state",
                progress=5 if can_retry else 100,
                attempt=attempt,
                trace_id=trace_id,
                message=message,
            )
            span.record_exception(exc)
            if can_retry:
                REPORT_RETRIES.inc()
                raise self.retry(exc=exc, countdown=2 ** attempt)
            record_report_task(
                "failure", "celery", _task_elapsed_seconds(task_id, attempt_started)
            )
            raise exc

        if state.get("final_report") and state.get("report_status") == "completed":
            _progress(
                state, task_id=task_id, status="completed", stage="completed",
                progress=100, attempt=attempt, trace_id=trace_id,
                message="评估报告已生成",
            )
            return _task_result(state)

        state["report_error"] = None
        try:
            from orchestration.nodes import (
                node_generate_report,
                node_save_memory,
                node_study_plan,
            )

            _progress(
                state, task_id=task_id, status="processing",
                stage="generate_report", progress=20, attempt=attempt,
                trace_id=trace_id, message="正在生成面试评估报告",
            )
            state = node_generate_report(state)
            _progress(
                state, task_id=task_id, status="processing",
                stage="study_plan", progress=60, attempt=attempt,
                trace_id=trace_id, message="正在生成个性化学习计划",
            )
            state = node_study_plan(state)
            _progress(
                state, task_id=task_id, status="processing",
                stage="save_memory", progress=85, attempt=attempt,
                trace_id=trace_id, message="正在沉淀跨会话能力画像",
            )
            state = node_save_memory(state)
            state["report_error"] = None
            _progress(
                state, task_id=task_id, status="completed", stage="completed",
                progress=100, attempt=attempt, trace_id=trace_id,
                message="评估报告已生成",
            )
            logger.info("Celery report task completed: session={}", session_id)
            record_report_task(
                "success", "celery", _task_elapsed_seconds(task_id, attempt_started)
            )
            return _task_result(state)
        except Exception as exc:
            can_retry = self.request.retries < self.max_retries
            status = "retrying" if can_retry else "failed"
            error_message = redact_sensitive_text(str(exc))[:500]
            state["report_error"] = (
                "报告生成暂时失败，正在重试"
                if can_retry else "报告生成失败，请稍后重试"
            )
            _progress(
                state, task_id=task_id, status=status, stage=status,
                progress=10 if can_retry else 100, attempt=attempt,
                trace_id=trace_id, message=state["report_error"],
            )
            background_task_repository.update(
                task_id,
                status=status,
                stage=status,
                progress=10 if can_retry else 100,
                attempt=attempt,
                trace_id=trace_id,
                error_type=type(exc).__name__,
                error_message=error_message,
            )
            span.record_exception(exc)
            logger.exception("Celery report task failed: session={}", session_id)
            if can_retry:
                REPORT_RETRIES.inc()
                raise self.retry(exc=exc, countdown=2 ** attempt)
            record_report_task(
                "failure", "celery", _task_elapsed_seconds(task_id, attempt_started)
            )
            raise


def enqueue_report(
    session_id: str, task_id: str, *, headers: Dict[str, str] | None = None
) -> str:
    """Publish a report task and return its Celery task id."""
    result = generate_report_task.apply_async(
        args=[session_id], task_id=task_id, headers=headers or {}
    )
    return str(result.id)


def _task_result(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": state.get("session_id", ""),
        "report": state.get("final_report", {}),
        "study_plan": state.get("study_plan", {}),
        "github_recommendations": state.get("github_recommendations", []),
    }
