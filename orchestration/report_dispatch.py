"""Dispatch final report generation to Celery with a synchronous fallback."""
from __future__ import annotations

import uuid
import time
from typing import Any, Dict

from loguru import logger
from opentelemetry.propagate import inject

from memory.memory_manager import memory_manager
from memory.task_repository import background_task_repository
from tasks.event_bus import publish_task_event
from observability.metrics import REPORT_FALLBACKS, record_report_task


def dispatch_report(state: Dict[str, Any]) -> Dict[str, Any]:
    """Queue an unfinished async report after its state has reached Redis."""
    if not state.get("async_report") or state.get("final_report"):
        return state
    if state.get("report_status") not in ("", None, "pending"):
        return state

    session_id = str(state.get("session_id", ""))
    if not session_id:
        return _finalize_synchronously(state, "missing session_id")

    task_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"interview-agent:report:{session_id}:v1"
    ))
    trace_id = str(state.get("runtime_trace_id") or "")
    task_record, created = background_task_repository.create_or_get(
        task_id=task_id,
        idempotency_key=f"report:{session_id}:v1",
        task_type="interview_report",
        session_id=session_id,
        user_id=str(state.get("user_id") or ""),
        trace_id=trace_id,
    )
    if not created and task_record.get("status") in {
        "queued", "processing", "retrying", "completed"
    }:
        state["report_status"] = task_record["status"]
        state["report_stage"] = task_record.get("stage", "")
        state["report_progress"] = task_record.get("progress", 0)
        state["report_task_id"] = task_record["task_id"]
        memory_manager.save_session(session_id, state)
        return state

    state["report_status"] = "queued"
    state["report_stage"] = "queued"
    state["report_progress"] = 5
    state["report_task_id"] = task_id
    state["report_error"] = None
    memory_manager.save_session(session_id, state)
    background_task_repository.update(
        task_id, status="queued", stage="queued", progress=5, trace_id=trace_id
    )
    publish_task_event(
        session_id=session_id,
        task_id=task_id,
        status="queued",
        stage="queued",
        progress=5,
        trace_id=trace_id,
        message="报告任务已进入队列",
    )

    try:
        from tasks.report_tasks import enqueue_report

        trace_headers: Dict[str, str] = {}
        inject(trace_headers)
        if trace_id:
            trace_headers["x-agent-trace-id"] = trace_id
        enqueue_report(session_id, task_id, headers=trace_headers)
        logger.info("Celery report task queued: session={}, task={}", session_id, task_id)
        return state
    except Exception as exc:
        logger.warning("Celery task publish failed; falling back to sync: {}", exc)
        return _finalize_synchronously(state, str(exc))


def _finalize_synchronously(state: Dict[str, Any], reason: str) -> Dict[str, Any]:
    from orchestration.nodes import (
        node_generate_report,
        node_save_memory,
        node_study_plan,
    )

    started = time.perf_counter()
    REPORT_FALLBACKS.labels(
        reason="missing_session" if reason == "missing session_id" else "publish_failure"
    ).inc()
    try:
        state["async_report"] = False
        state["report_status"] = "processing"
        state["report_stage"] = "synchronous_fallback"
        state["report_progress"] = 10
        state["report_error"] = reason
        state = node_generate_report(state)
        state = node_study_plan(state)
        state = node_save_memory(state)
        state["report_status"] = "completed"
        state["report_stage"] = "completed"
        state["report_progress"] = 100
        state["report_error"] = None
        memory_manager.save_session(str(state.get("session_id", "")), state)
        task_id = str(state.get("report_task_id") or "")
        if task_id:
            background_task_repository.update(
                task_id, status="completed", stage="completed", progress=100
            )
            publish_task_event(
                session_id=str(state.get("session_id") or ""),
                task_id=task_id,
                status="completed",
                stage="completed",
                progress=100,
                trace_id=str(state.get("runtime_trace_id") or ""),
                message="同步降级已完成报告生成",
            )
        record_report_task(
            "success", "synchronous_fallback", time.perf_counter() - started
        )
        return state
    except Exception:
        record_report_task(
            "failure", "synchronous_fallback", time.perf_counter() - started
        )
        raise
