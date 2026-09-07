"""Shared LangGraph checkpoint backend tests."""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from orchestration.checkpointing import SQLAlchemyCheckpointSaver


class CounterState(TypedDict):
    value: int


def _shared_engine():
    return create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_sqlalchemy_checkpointer_persists_and_resumes_across_saver_instances():
    engine = _shared_engine()
    first_saver = SQLAlchemyCheckpointSaver(engine)
    workflow = StateGraph(CounterState)
    workflow.add_node("increment", lambda state: {"value": state["value"] + 1})
    workflow.add_edge(START, "increment")
    workflow.add_edge("increment", END)
    graph = workflow.compile(checkpointer=first_saver)
    config = {"configurable": {"thread_id": "shared-session"}}

    assert graph.invoke({"value": 1}, config)["value"] == 2

    # A different API worker creates its own saver/connection pool but reads
    # the same database tables and therefore sees the same LangGraph thread.
    second_saver = SQLAlchemyCheckpointSaver(engine)
    resumed = second_saver.get_tuple(config)
    assert resumed is not None
    assert resumed.checkpoint["channel_values"]["value"] == 2

    second_saver.delete_thread("shared-session")
    assert first_saver.get_tuple(config) is None

