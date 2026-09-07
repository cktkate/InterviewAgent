"""Agent Runtime tracing and invocation contract tests."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from typing import TypedDict

from opentelemetry.sdk.trace import TracerProvider
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from observability.tracing import JsonlSpanExporter
from orchestration.runtime import (
    AgentRuntimeGraph,
    RUNTIME_RUN_ID,
    RUNTIME_TRACE_ID,
)


class _FakeCompiledGraph:
    def __init__(self) -> None:
        self.config = None

    def invoke(self, state, config=None, **kwargs):
        self.config = config
        return dict(state, result="ok")


def test_runtime_adds_run_and_trace_ids(monkeypatch):
    from orchestration import runtime

    provider = TracerProvider()
    monkeypatch.setattr(runtime, "get_tracer", lambda: provider.get_tracer("test"))
    monkeypatch.setattr(runtime, "_configure_tracing", lambda: None)

    compiled = _FakeCompiledGraph()
    graph = AgentRuntimeGraph(compiled)
    result = graph.invoke({"session_id": "session-1", "intent": "test"})

    assert result[RUNTIME_RUN_ID]
    assert len(result[RUNTIME_TRACE_ID]) == 32
    assert compiled.config["configurable"]["thread_id"] == "session-1"
    assert compiled.config["metadata"]["run_id"] == result[RUNTIME_RUN_ID]
    assert compiled.config["recursion_limit"] == runtime.settings.agent_max_steps
    assert result["runtime_usage"]["llm_calls"] == 0


def test_jsonl_exporter_does_not_include_business_payload(tmp_path):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    output = tmp_path / "traces.jsonl"
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(output)))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("agent.node.test") as span:
        span.set_attribute("agent.node.name", "test")
        span.set_attribute("agent.input.answer_length", 42)

    record = json.loads(output.read_text(encoding="utf-8").strip())
    encoded = json.dumps(record, ensure_ascii=False)
    assert record["name"] == "agent.node.test"
    assert record["attributes"]["agent.input.answer_length"] == 42
    assert "resume_text" not in encoded
    assert "user_answer" not in encoded


def test_runtime_uses_session_as_persistent_checkpoint_thread(tmp_path, monkeypatch):
    from orchestration import runtime

    provider = TracerProvider()
    monkeypatch.setattr(runtime, "get_tracer", lambda: provider.get_tracer("test"))
    monkeypatch.setattr(runtime, "_configure_tracing", lambda: None)

    class State(TypedDict, total=False):
        session_id: str
        value: int
        runtime_run_id: str
        runtime_trace_id: str

    workflow = StateGraph(State)
    workflow.add_node("increment", lambda state: {"value": state.get("value", 0) + 1})
    workflow.add_edge(START, "increment")
    workflow.add_edge("increment", END)

    connection = sqlite3.connect(tmp_path / "checkpoint.sqlite3", check_same_thread=False)
    graph = AgentRuntimeGraph(workflow.compile(checkpointer=SqliteSaver(connection)))
    result = graph.invoke({"session_id": "session-checkpoint", "value": 1})
    history = list(
        graph.get_state_history({"configurable": {"thread_id": "session-checkpoint"}})
    )

    assert result["value"] == 2
    assert len(history) == 3
    assert history[0].values["runtime_run_id"] == result[RUNTIME_RUN_ID]


def test_runtime_loads_and_updates_durable_checkpoint_state(tmp_path, monkeypatch):
    from orchestration import runtime

    monkeypatch.setattr(runtime.settings, "observability_enabled", False)
    monkeypatch.setattr(runtime, "_configure_tracing", lambda: None)

    class State(TypedDict, total=False):
        session_id: str
        value: int
        report_status: str

    workflow = StateGraph(State)
    workflow.add_node("finish", lambda state: {"value": state.get("value", 0) + 1})
    workflow.add_edge(START, "finish")
    workflow.add_edge("finish", END)

    connection = sqlite3.connect(tmp_path / "durable.sqlite3", check_same_thread=False)
    graph = AgentRuntimeGraph(
        workflow.compile(checkpointer=SqliteSaver(connection)),
        checkpoint_enabled=True,
    )
    graph.invoke({"session_id": "durable-session", "value": 1})

    assert graph.save_checkpoint_state(
        "durable-session",
        {"report_status": "completed"},
    )
    restored = graph.load_checkpoint_state("durable-session")

    assert restored["value"] == 2
    assert restored["report_status"] == "completed"


def test_runtime_astream_bridges_sync_graph_without_blocking_event_loop(monkeypatch):
    from orchestration import runtime

    main_thread = threading.get_ident()

    class FakeStreamingGraph:
        def __init__(self):
            self.worker_thread = None
            self.config = None

        def stream(self, state, config=None, **kwargs):
            self.worker_thread = threading.get_ident()
            self.config = config
            yield {"route": dict(state, intent="start_interview")}
            yield {"ask": {"current_question_text": "什么是 RAG？"}}

    monkeypatch.setattr(runtime.settings, "observability_enabled", False)
    monkeypatch.setattr(runtime, "_configure_tracing", lambda: None)
    compiled = FakeStreamingGraph()
    graph = AgentRuntimeGraph(compiled)

    async def collect():
        return [
            item async for item in graph.astream(
                {"session_id": "stream-session"}, stream_mode="updates"
            )
        ]

    updates = asyncio.run(collect())
    assert [next(iter(item)) for item in updates] == ["route", "ask"]
    assert compiled.worker_thread != main_thread
    assert compiled.config["configurable"]["thread_id"] == "stream-session"


def test_runtime_astream_keeps_sync_sqlite_checkpoint_support(tmp_path, monkeypatch):
    from orchestration import runtime

    monkeypatch.setattr(runtime.settings, "observability_enabled", False)
    monkeypatch.setattr(runtime, "_configure_tracing", lambda: None)

    class State(TypedDict, total=False):
        session_id: str
        value: int
        runtime_run_id: str

    workflow = StateGraph(State)
    workflow.add_node("increment", lambda state: {"value": state.get("value", 0) + 1})
    workflow.add_edge(START, "increment")
    workflow.add_edge("increment", END)

    connection = sqlite3.connect(tmp_path / "stream.sqlite3", check_same_thread=False)
    graph = AgentRuntimeGraph(workflow.compile(checkpointer=SqliteSaver(connection)))

    async def collect():
        return [
            item async for item in graph.astream(
                {"session_id": "sqlite-stream", "value": 1},
                stream_mode="updates",
            )
        ]

    updates = asyncio.run(collect())
    assert updates[-1]["increment"]["value"] == 2
    history = list(
        graph.get_state_history({"configurable": {"thread_id": "sqlite-stream"}})
    )
    assert history[0].values["value"] == 2
