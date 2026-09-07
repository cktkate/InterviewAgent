"""Policy, reliability and MCP integration tests for Tool Runtime."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field

from mcp.mcp_client import MCPClient
from tool_runtime.core import (
    RetryableToolError,
    RiskLevel,
    ToolContext,
    ToolDefinition,
    ToolExecutionFailure,
    ToolExecutor,
    ToolRegistry,
)


class SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=20)


def _executor(definition: ToolDefinition) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(definition)
    return ToolExecutor(registry)


def _read_context() -> ToolContext:
    return ToolContext(
        user_id="user-1",
        session_id="session-1",
        permissions=frozenset({"external:read"}),
    )


def test_schema_validation_rejects_unknown_and_oversized_arguments():
    called = []
    executor = _executor(ToolDefinition(
        name="test.search",
        description="test",
        handler=lambda query: called.append(query),
        argument_model=SearchArguments,
        required_permissions=frozenset({"external:read"}),
    ))

    unknown = executor.execute(
        "test.search", {"query": "ok", "command": "delete"}, context=_read_context()
    )
    oversized = executor.execute(
        "test.search", {"query": "x" * 21}, context=_read_context()
    )
    assert unknown.error_code == "invalid_arguments"
    assert oversized.error_code == "invalid_arguments"
    assert called == []


def test_permissions_and_high_risk_confirmation_are_enforced():
    executor = _executor(ToolDefinition(
        name="test.delete",
        description="test",
        handler=lambda: "deleted",
        input_schema={"type": "object", "additionalProperties": False},
        risk_level=RiskLevel.WRITE_HIGH,
        required_permissions=frozenset({"external:write"}),
    ))

    denied = executor.execute("test.delete", {}, context=_read_context())
    confirmation = executor.execute(
        "test.delete",
        {},
        context=ToolContext(permissions=frozenset({"external:write"})),
    )
    approved = executor.execute(
        "test.delete",
        {},
        context=ToolContext(
            permissions=frozenset({"external:write"}),
            approved_risks=frozenset({RiskLevel.WRITE_HIGH.value}),
        ),
    )
    assert denied.error_code == "permission_denied"
    assert confirmation.error_code == "confirmation_required"
    assert approved.ok and approved.data == "deleted"


def test_retry_timeout_circuit_breaker_and_idempotency():
    calls = {"count": 0}

    def flaky(query: str):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RetryableToolError("temporary")
        return query.upper()

    executor = _executor(ToolDefinition(
        name="test.flaky",
        description="test",
        handler=flaky,
        argument_model=SearchArguments,
        required_permissions=frozenset({"external:read"}),
        max_retries=1,
    ))
    result = executor.execute(
        "test.flaky", {"query": "rag"}, context=_read_context(), idempotency_key="same"
    )
    replay = executor.execute(
        "test.flaky", {"query": "ignored"}, context=_read_context(), idempotency_key="same"
    )
    assert result.ok and result.attempts == 2
    assert replay.ok and replay.data == "RAG"
    assert replay.audit["idempotency_replay"] is True
    assert calls["count"] == 2

    timeout_executor = _executor(ToolDefinition(
        name="test.slow",
        description="test",
        handler=lambda: time.sleep(0.15),
        input_schema={"type": "object", "additionalProperties": False},
        timeout_seconds=0.01,
        max_retries=0,
    ))
    timed_out = timeout_executor.execute("test.slow", {})
    assert timed_out.error_code == "external_transient_error"
    assert timed_out.duration_ms < 100

    failing_executor = _executor(ToolDefinition(
        name="test.broken",
        description="test",
        handler=lambda: (_ for _ in ()).throw(RetryableToolError("down")),
        input_schema={"type": "object", "additionalProperties": False},
        max_retries=0,
        circuit_breaker_threshold=2,
        circuit_open_seconds=10,
    ))
    assert failing_executor.execute("test.broken", {}).status == "failed"
    assert failing_executor.execute("test.broken", {}).status == "failed"
    circuit = failing_executor.execute("test.broken", {})
    assert circuit.error_code == "circuit_open"


def test_non_retryable_failure_stops_immediately():
    calls = {"count": 0}

    def fail():
        calls["count"] += 1
        raise ToolExecutionFailure("bad request", code="external_http_error")

    executor = _executor(ToolDefinition(
        name="test.bad",
        description="test",
        handler=fail,
        input_schema={"type": "object", "additionalProperties": False},
        max_retries=3,
    ))
    result = executor.execute("test.bad", {})
    assert result.error_code == "external_http_error"
    assert result.attempts == 1
    assert calls["count"] == 1


def test_rate_limit_and_audit_metadata_do_not_store_raw_arguments():
    executor = _executor(ToolDefinition(
        name="test.limited",
        description="test",
        handler=lambda query: {"ok": True},
        argument_model=SearchArguments,
        rate_limit_per_minute=1,
    ))
    first = executor.execute("test.limited", {"query": "secret-value"})
    second = executor.execute("test.limited", {"query": "another"})
    assert first.ok
    assert second.error_code == "rate_limited"
    assert first.audit["argument_keys"] == ["query"]
    assert "secret-value" not in str(first.audit)
    assert executor.recent_events[-1]["status"] == "rejected"


def test_shared_state_makes_idempotency_and_rate_limits_cross_executor():
    class SharedState:
        def __init__(self):
            self.cache = {}
            self.calls = {}

        def check_limits(self, tool_name, *, rate_limit_per_minute):
            count = self.calls.get(tool_name, 0)
            if count >= rate_limit_per_minute:
                return "rate_limited", "tool rate limit exceeded"
            self.calls[tool_name] = count + 1
            return None

        def record_success(self, tool_name):
            return None

        def record_failure(self, tool_name, *, threshold, open_seconds):
            return None

        def get_idempotency(self, tool_name, key):
            return self.cache.get((tool_name, key))

        def set_idempotency(self, tool_name, key, result):
            self.cache[(tool_name, key)] = dict(result)

    calls = {"count": 0}

    def handler(query):
        calls["count"] += 1
        return query.upper()

    definition = ToolDefinition(
        name="test.shared",
        description="test",
        handler=handler,
        argument_model=SearchArguments,
        rate_limit_per_minute=1,
    )
    registry = ToolRegistry()
    registry.register(definition)
    shared = SharedState()
    worker_a = ToolExecutor(registry, state_store=shared)
    worker_b = ToolExecutor(registry, state_store=shared)

    original = worker_a.execute(
        "test.shared", {"query": "rag"}, idempotency_key="request-1"
    )
    replay = worker_b.execute(
        "test.shared", {"query": "ignored"}, idempotency_key="request-1"
    )
    limited = worker_b.execute("test.shared", {"query": "new"})

    assert original.ok
    assert replay.ok and replay.data == "RAG"
    assert replay.audit["idempotency_replay"] is True
    assert limited.error_code == "rate_limited"
    assert calls["count"] == 1


def test_idempotency_cache_is_scoped_by_authenticated_user():
    calls = {"count": 0}

    def handler(query):
        calls["count"] += 1
        return query

    executor = _executor(ToolDefinition(
        name="test.tenant-idempotency",
        description="test",
        handler=handler,
        argument_model=SearchArguments,
        rate_limit_per_minute=10,
    ))
    user_a = ToolContext(user_id="user-a")
    user_b = ToolContext(user_id="user-b")

    first = executor.execute(
        "test.tenant-idempotency",
        {"query": "private-a"},
        context=user_a,
        idempotency_key="same-request-id",
    )
    second = executor.execute(
        "test.tenant-idempotency",
        {"query": "private-b"},
        context=user_b,
        idempotency_key="same-request-id",
    )

    assert first.data == "private-a"
    assert second.data == "private-b"
    assert calls["count"] == 2


def test_mcp_tools_require_discovery_schema_and_policy():
    class FakeSession:
        def __init__(self):
            self.calls = []

        async def list_tools(self):
            return SimpleNamespace(tools=[
                SimpleNamespace(
                    name="lookup",
                    description="read data",
                    inputSchema={
                        "type": "object",
                        "properties": {"query": {"type": "string", "maxLength": 20}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                    annotations=SimpleNamespace(
                        readOnlyHint=True, destructiveHint=False
                    ),
                )
            ])

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return SimpleNamespace(content=[{"value": arguments["query"]}])

    async def scenario():
        client = MCPClient()
        session = FakeSession()
        discovered = await client.list_tools(session)
        assert discovered[0]["risk_level"] == "read_only"
        content = await client.call_tool(session, "lookup", {"query": "RAG"})
        assert content == [{"value": "RAG"}]
        try:
            await client.call_tool(
                session, "lookup", {"query": "RAG", "command": "unsafe"}
            )
        except RuntimeError as exc:
            assert "invalid_arguments" in str(exc)
        else:
            raise AssertionError("invalid MCP arguments were not rejected")
        assert len(session.calls) == 1

    asyncio.run(scenario())
