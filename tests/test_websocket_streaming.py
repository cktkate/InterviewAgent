"""Hybrid WebSocket streaming protocol tests."""
from __future__ import annotations

import asyncio
import json


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages = []

    async def send_text(self, raw: str) -> None:
        self.messages.append(json.loads(raw))


class _FakeStreamingGraph:
    async def astream(self, state, stream_mode="updates"):
        assert stream_mode == "updates"
        yield {"route": {"intent": "start_interview", "runtime_run_id": "run-1"}}
        yield {"rag_retrieve": {"rag_sources": [{"id": "doc-1"}]}}
        yield {"ask": {"current_question_text": "请解释混合检索。"}}


def test_stream_graph_emits_node_progress_and_returns_merged_state():
    from api.routers import websocket as websocket_router

    ws = _FakeWebSocket()
    state = asyncio.run(websocket_router._stream_graph(
        ws,
        _FakeStreamingGraph(),
        {"session_id": "session-1"},
        "start_interview",
    ))

    assert [message["type"] for message in ws.messages] == [
        "agent_stream_start",
        "agent_status",
        "agent_status",
        "agent_status",
        "agent_stream_end",
    ]
    assert [
        message["node"] for message in ws.messages
        if message["type"] == "agent_status"
    ] == ["route", "rag_retrieve", "ask"]
    assert state["current_question_text"] == "请解释混合检索。"
    assert ws.messages[-1]["run_id"] == "run-1"
    assert [message["progress"] for message in ws.messages] == [3, 5, 65, 100, 100]
    assert ws.messages[2]["message"] == "知识检索完成"


def test_text_stream_reconstructs_validated_text_and_returns_stream_id():
    from api.routers import websocket as websocket_router

    ws = _FakeWebSocket()
    text = "结构化结果校验完成后，再按文本块渐进推送。"
    stream_id = asyncio.run(websocket_router._send_text_stream(
        ws, text, "question", {"index": 1, "total": 5}, chunk_size=5
    ))

    assert ws.messages[0]["type"] == "text_stream_start"
    assert ws.messages[0]["index"] == 1
    deltas = [
        message["delta"] for message in ws.messages
        if message["type"] == "text_stream_delta"
    ]
    assert "".join(deltas) == text
    assert ws.messages[-1] == {
        "type": "text_stream_end",
        "stream_id": stream_id,
        "kind": "question",
        "text": text,
    }


def test_farewell_does_not_repeat_the_last_interview_question():
    from api.routers import websocket as websocket_router

    last_question = "如果需要优化 RAG 检索速度，你会从哪些方面入手？"
    state = {
        "interview_finished": True,
        "current_question_text": last_question,
        "agent_reply": last_question,
    }

    farewell = websocket_router._resolve_farewell(state)
    assert farewell != last_question
    assert "感谢" in farewell


def test_background_task_event_is_forwarded_to_websocket(monkeypatch):
    from api.routers import websocket as websocket_router
    from tasks import event_bus

    async def fake_events(session_id):
        assert session_id == "session-1"
        yield {
            "event_id": "1-0",
            "task_id": "task-1",
            "status": "processing",
            "stage": "study_plan",
            "progress": 60,
            "attempt": 1,
            "message": "正在生成个性化学习计划",
        }

    monkeypatch.setattr(event_bus, "stream_task_events", fake_events)
    ws = _FakeWebSocket()

    asyncio.run(websocket_router._forward_task_events(ws, "session-1"))

    assert ws.messages == [{
        "type": "report_progress",
        "task_id": "task-1",
        "status": "processing",
        "stage": "study_plan",
        "progress": 60,
        "attempt": 1,
        "message": "正在生成个性化学习计划",
        "event_id": "1-0",
    }]
