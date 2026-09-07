"""Deterministic, policy-gated runtime for local and MCP tools."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional, Type

from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, ValidationError

from config import settings
from observability.tracing import configure_tracing, get_tracer
from tool_runtime.state_store import ToolStateStore, create_tool_state_store


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    WRITE_LOW = "write_low"
    WRITE_HIGH = "write_high"


class ToolRuntimeError(RuntimeError):
    code = "tool_error"
    retryable = False


class RetryableToolError(ToolRuntimeError):
    code = "external_transient_error"
    retryable = True


class ToolExecutionFailure(ToolRuntimeError):
    def __init__(self, message: str, *, code: str = "tool_execution_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolContext:
    user_id: str = "system"
    session_id: str = ""
    trace_id: str = ""
    agent_role: str = ""
    policy_version: str = ""
    permissions: frozenset[str] = field(default_factory=frozenset)
    approved_risks: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def system_readonly(cls, *, session_id: str = "", trace_id: str = "") -> "ToolContext":
        return cls(
            user_id="system",
            session_id=session_id,
            trace_id=trace_id,
            permissions=frozenset({"external:read"}),
        )


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    handler: Callable[..., Any]
    argument_model: Optional[Type[BaseModel]] = None
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.READ_ONLY
    required_permissions: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: float = 10.0
    max_retries: int = 1
    idempotent: bool = True
    rate_limit_per_minute: int = 30
    circuit_breaker_threshold: int = 3
    circuit_open_seconds: float = 30.0


@dataclass
class ToolResult:
    ok: bool
    tool_call_id: str
    tool_name: str
    status: str
    data: Any = None
    error_code: str = ""
    error_message: str = ""
    attempts: int = 0
    duration_ms: float = 0.0
    audit: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> Dict[str, Any]:
        result_count = len(self.data) if isinstance(self.data, (list, tuple, dict)) else None
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "error_code": self.error_code,
            "attempts": self.attempts,
            "duration_ms": round(self.duration_ms, 3),
            "result_count": result_count,
            "risk_level": self.audit.get("risk_level", ""),
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: Dict[str, ToolDefinition] = {}
        self._lock = threading.RLock()

    def register(self, definition: ToolDefinition, *, replace: bool = False) -> None:
        if (
            not definition.name
            or len(definition.name) > 128
            or any(not (char.isalnum() or char in "._-") for char in definition.name)
        ):
            raise ValueError("invalid tool name")
        with self._lock:
            if definition.name in self._definitions and not replace:
                raise ValueError(f"tool already registered: {definition.name}")
            self._definitions[definition.name] = definition

    def get(self, name: str) -> Optional[ToolDefinition]:
        with self._lock:
            return self._definitions.get(name)

    def list_tools(self) -> list[Dict[str, Any]]:
        with self._lock:
            definitions = list(self._definitions.values())
        return [
            {
                "name": item.name,
                "description": item.description,
                "input_schema": (
                    item.argument_model.model_json_schema()
                    if item.argument_model is not None
                    else dict(item.input_schema)
                ),
                "risk_level": item.risk_level.value,
                "required_permissions": sorted(item.required_permissions),
                "timeout_seconds": item.timeout_seconds,
                "idempotent": item.idempotent,
            }
            for item in definitions
        ]


def _validate_schema_value(value: Any, schema: Mapping[str, Any], path: str) -> Any:
    expected = schema.get("type")
    if isinstance(expected, list):
        expected = next((item for item in expected if item != "null"), None)
        if value is None and "null" in schema.get("type", []):
            return value

    checks = {
        "object": lambda item: isinstance(item, Mapping),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
    }
    if expected in checks and not checks[expected](value):
        raise ValueError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed value")

    if expected == "object":
        properties = dict(schema.get("properties") or {})
        required = set(schema.get("required") or [])
        missing = sorted(key for key in required if key not in value)
        if missing:
            raise ValueError(f"{path} missing required fields: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ValueError(f"{path} contains unknown fields: {', '.join(unknown)}")
        return {
            key: _validate_schema_value(item, properties[key], f"{path}.{key}")
            if key in properties else item
            for key, item in value.items()
        }
    if expected == "array":
        max_items = int(schema.get("maxItems", 100))
        if len(value) > max_items:
            raise ValueError(f"{path} has too many items")
        item_schema = dict(schema.get("items") or {})
        return [
            _validate_schema_value(item, item_schema, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if expected == "string":
        if len(value) < int(schema.get("minLength", 0)):
            raise ValueError(f"{path} is too short")
        if len(value) > int(schema.get("maxLength", 4096)):
            raise ValueError(f"{path} is too long")
    if expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} exceeds maximum")
    return value


def validate_arguments(definition: ToolDefinition, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise ValueError("tool arguments must be an object")
    if definition.argument_model is not None:
        return definition.argument_model.model_validate(dict(arguments)).model_dump()
    schema = dict(definition.input_schema or {"type": "object"})
    validated = _validate_schema_value(dict(arguments), schema, "arguments")
    return dict(validated)


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        state_store: Optional[ToolStateStore] = None,
    ) -> None:
        self.registry = registry
        self._state_store = state_store or create_tool_state_store()
        self._lock = threading.RLock()
        self._calls: Dict[str, deque[float]] = defaultdict(deque)
        self._failures: Dict[str, int] = defaultdict(int)
        self._open_until: Dict[str, float] = defaultdict(float)
        self._idempotency_cache: Dict[tuple[str, str, str], ToolResult] = {}
        self.recent_events: deque[Dict[str, Any]] = deque(maxlen=200)

    @staticmethod
    def _failure(
        tool_name: str,
        call_id: str,
        status: str,
        code: str,
        message: str,
        *,
        risk_level: str = "",
        started: Optional[float] = None,
    ) -> ToolResult:
        duration = ((time.perf_counter() - started) * 1000) if started else 0.0
        return ToolResult(
            ok=False,
            tool_call_id=call_id,
            tool_name=tool_name,
            status=status,
            error_code=code,
            error_message=message[:300],
            duration_ms=duration,
            audit={"risk_level": risk_level},
        )

    def _authorize(self, definition: ToolDefinition, context: ToolContext) -> Optional[ToolResult]:
        missing = definition.required_permissions - context.permissions
        call_id = str(uuid.uuid4())
        if missing:
            return self._failure(
                definition.name, call_id, "denied", "permission_denied",
                f"missing permissions: {', '.join(sorted(missing))}",
                risk_level=definition.risk_level.value,
            )
        if (
            definition.risk_level == RiskLevel.WRITE_HIGH
            and RiskLevel.WRITE_HIGH.value not in context.approved_risks
        ):
            return self._failure(
                definition.name, call_id, "confirmation_required",
                "confirmation_required", "high-risk tool requires explicit approval",
                risk_level=definition.risk_level.value,
            )
        return None

    def _check_limits(self, definition: ToolDefinition, now: float) -> Optional[tuple[str, str]]:
        if self._state_store is not None:
            return self._state_store.check_limits(
                definition.name,
                rate_limit_per_minute=definition.rate_limit_per_minute,
            )
        with self._lock:
            if self._open_until[definition.name] > now:
                return "circuit_open", "tool circuit breaker is open"
            calls = self._calls[definition.name]
            while calls and calls[0] <= now - 60.0:
                calls.popleft()
            if len(calls) >= definition.rate_limit_per_minute:
                return "rate_limited", "tool rate limit exceeded"
            calls.append(now)
        return None

    def _record_success(self, name: str) -> None:
        if self._state_store is not None:
            self._state_store.record_success(name)
            return
        with self._lock:
            self._failures[name] = 0
            self._open_until[name] = 0.0

    def _record_failure(self, definition: ToolDefinition) -> None:
        if self._state_store is not None:
            self._state_store.record_failure(
                definition.name,
                threshold=definition.circuit_breaker_threshold,
                open_seconds=definition.circuit_open_seconds,
            )
            return
        with self._lock:
            self._failures[definition.name] += 1
            if self._failures[definition.name] >= definition.circuit_breaker_threshold:
                self._open_until[definition.name] = time.monotonic() + definition.circuit_open_seconds

    def _get_idempotency(
        self, cache_key: tuple[str, str, str]
    ) -> Optional[ToolResult]:
        tool_name, user_id, idempotency_key = cache_key
        if self._state_store is not None:
            payload = self._state_store.get_idempotency(
                tool_name, f"{user_id}:{idempotency_key}"
            )
            return ToolResult(**payload) if payload else None
        with self._lock:
            return self._idempotency_cache.get(cache_key)

    def _set_idempotency(
        self, cache_key: tuple[str, str, str], result: ToolResult
    ) -> None:
        tool_name, user_id, idempotency_key = cache_key
        if self._state_store is not None:
            self._state_store.set_idempotency(
                tool_name, f"{user_id}:{idempotency_key}", result.__dict__
            )
            return
        with self._lock:
            self._idempotency_cache[cache_key] = result

    @staticmethod
    def _invoke_sync(definition: ToolDefinition, arguments: Dict[str, Any]) -> Any:
        result = definition.handler(**arguments)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    @staticmethod
    async def _invoke_async(definition: ToolDefinition, arguments: Dict[str, Any]) -> Any:
        if inspect.iscoroutinefunction(definition.handler):
            return await definition.handler(**arguments)
        result = await asyncio.to_thread(definition.handler, **arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    def execute(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        context: Optional[ToolContext] = None,
        idempotency_key: str = "",
        definition_override: Optional[ToolDefinition] = None,
    ) -> ToolResult:
        definition = definition_override or self.registry.get(tool_name)
        call_id = str(uuid.uuid4())
        started = time.perf_counter()
        if definition is None:
            return self._finalize(self._failure(
                tool_name, call_id, "rejected", "unknown_tool", "tool is not registered",
                started=started,
            ))
        context = context or ToolContext()
        denied = self._authorize(definition, context)
        if denied:
            denied.tool_call_id = call_id
            return self._finalize(denied)
        try:
            validated = validate_arguments(definition, arguments)
        except (ValidationError, ValueError, TypeError) as exc:
            return self._finalize(self._failure(
                definition.name, call_id, "rejected", "invalid_arguments", str(exc),
                risk_level=definition.risk_level.value, started=started,
            ))

        # The per-run Harness budget is optional outside a graph execution.
        # Import lazily to keep Tool Runtime usable as a standalone component.
        from orchestration.budget import before_tool_call

        before_tool_call()

        cache_key = (definition.name, context.user_id, idempotency_key)
        if definition.idempotent and idempotency_key:
            cached = self._get_idempotency(cache_key)
            if cached is not None:
                duplicate = ToolResult(**{**cached.__dict__, "tool_call_id": call_id})
                duplicate.audit = {**cached.audit, "idempotency_replay": True}
                return self._finalize(duplicate)

        limited = self._check_limits(definition, time.monotonic())
        if limited:
            code, message = limited
            return self._finalize(self._failure(
                definition.name, call_id, "rejected", code, message,
                risk_level=definition.risk_level.value, started=started,
            ))
        return self._run_sync(
            definition, validated, context, call_id, started, cache_key, bool(idempotency_key)
        )

    def _run_sync(
        self,
        definition: ToolDefinition,
        arguments: Dict[str, Any],
        context: ToolContext,
        call_id: str,
        started: float,
        cache_key: tuple[str, str, str],
        cache_enabled: bool,
    ) -> ToolResult:
        input_digest = hashlib.sha256(
            json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        attempts = 0
        error: Optional[BaseException] = None
        self._configure_tracing()
        tracer = get_tracer()
        with tracer.start_as_current_span(f"agent.tool.{definition.name}") as span:
            self._set_span_input(span, definition, context, call_id, arguments, input_digest)
            for attempt in range(definition.max_retries + 1):
                attempts = attempt + 1
                pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-tool")
                future = pool.submit(self._invoke_sync, definition, arguments)
                try:
                    data = future.result(timeout=definition.timeout_seconds)
                    pool.shutdown(wait=False, cancel_futures=True)
                    self._record_success(definition.name)
                    result = ToolResult(
                        ok=True, tool_call_id=call_id, tool_name=definition.name,
                        status="success", data=data, attempts=attempts,
                        duration_ms=(time.perf_counter() - started) * 1000,
                        audit={
                            "risk_level": definition.risk_level.value,
                            "argument_keys": sorted(arguments),
                            "input_digest": input_digest,
                            "user_id": context.user_id,
                            "session_id": context.session_id,
                        },
                    )
                    if cache_enabled:
                        self._set_idempotency(cache_key, result)
                    self._set_span_result(span, result)
                    return self._finalize(result)
                except FutureTimeoutError:
                    future.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    error = RetryableToolError("tool execution timed out")
                except BaseException as exc:
                    pool.shutdown(wait=False, cancel_futures=True)
                    error = exc
                if not getattr(error, "retryable", True) or attempt >= definition.max_retries:
                    break
                time.sleep(min(0.1 * (2 ** attempt), 1.0))

            self._record_failure(definition)
            code = str(getattr(error, "code", "tool_execution_failed"))
            result = self._failure(
                definition.name, call_id, "failed", code, str(error or "tool failed"),
                risk_level=definition.risk_level.value, started=started,
            )
            result.attempts = attempts
            self._set_span_result(span, result, error)
            return self._finalize(result)

    async def aexecute(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        context: Optional[ToolContext] = None,
        idempotency_key: str = "",
        definition_override: Optional[ToolDefinition] = None,
    ) -> ToolResult:
        definition = definition_override or self.registry.get(tool_name)
        call_id = str(uuid.uuid4())
        started = time.perf_counter()
        if definition is None:
            return self._finalize(self._failure(
                tool_name, call_id, "rejected", "unknown_tool", "tool is not registered",
                started=started,
            ))
        context = context or ToolContext()
        denied = self._authorize(definition, context)
        if denied:
            denied.tool_call_id = call_id
            return self._finalize(denied)
        try:
            validated = validate_arguments(definition, arguments)
        except (ValidationError, ValueError, TypeError) as exc:
            return self._finalize(self._failure(
                definition.name, call_id, "rejected", "invalid_arguments", str(exc),
                risk_level=definition.risk_level.value, started=started,
            ))
        from orchestration.budget import before_tool_call

        before_tool_call()
        cache_key = (definition.name, context.user_id, idempotency_key)
        if definition.idempotent and idempotency_key:
            cached = self._get_idempotency(cache_key)
            if cached is not None:
                duplicate = ToolResult(**{**cached.__dict__, "tool_call_id": call_id})
                duplicate.audit = {**cached.audit, "idempotency_replay": True}
                return self._finalize(duplicate)
        limited = self._check_limits(definition, time.monotonic())
        if limited:
            code, message = limited
            return self._finalize(self._failure(
                definition.name, call_id, "rejected", code, message,
                risk_level=definition.risk_level.value, started=started,
            ))

        input_digest = hashlib.sha256(
            json.dumps(validated, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        attempts = 0
        error: Optional[BaseException] = None
        self._configure_tracing()
        tracer = get_tracer()
        with tracer.start_as_current_span(f"agent.tool.{definition.name}") as span:
            self._set_span_input(span, definition, context, call_id, validated, input_digest)
            for attempt in range(definition.max_retries + 1):
                attempts = attempt + 1
                try:
                    data = await asyncio.wait_for(
                        self._invoke_async(definition, validated),
                        timeout=definition.timeout_seconds,
                    )
                    self._record_success(definition.name)
                    result = ToolResult(
                        ok=True, tool_call_id=call_id, tool_name=definition.name,
                        status="success", data=data, attempts=attempts,
                        duration_ms=(time.perf_counter() - started) * 1000,
                        audit={
                            "risk_level": definition.risk_level.value,
                            "argument_keys": sorted(validated),
                            "input_digest": input_digest,
                            "user_id": context.user_id,
                            "session_id": context.session_id,
                        },
                    )
                    if definition.idempotent and idempotency_key:
                        self._set_idempotency(cache_key, result)
                    self._set_span_result(span, result)
                    return self._finalize(result)
                except asyncio.TimeoutError:
                    error = RetryableToolError("tool execution timed out")
                except BaseException as exc:
                    error = exc
                if not getattr(error, "retryable", True) or attempt >= definition.max_retries:
                    break
                await asyncio.sleep(min(0.1 * (2 ** attempt), 1.0))

            self._record_failure(definition)
            code = str(getattr(error, "code", "tool_execution_failed"))
            result = self._failure(
                definition.name, call_id, "failed", code, str(error or "tool failed"),
                risk_level=definition.risk_level.value, started=started,
            )
            result.attempts = attempts
            self._set_span_result(span, result, error)
            return self._finalize(result)

    @staticmethod
    def _configure_tracing() -> None:
        configure_tracing(
            enabled=settings.observability_enabled,
            service_name=settings.otel_service_name,
            jsonl_enabled=settings.trace_jsonl_enabled,
            jsonl_path=settings.trace_jsonl_path,
            otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        )

    @staticmethod
    def _set_span_input(span, definition, context, call_id, arguments, digest) -> None:
        span.set_attribute("agent.tool.call_id", call_id)
        span.set_attribute("agent.tool.name", definition.name)
        span.set_attribute("agent.tool.risk_level", definition.risk_level.value)
        span.set_attribute("agent.tool.argument_keys", ",".join(sorted(arguments)))
        span.set_attribute("agent.tool.input_digest", digest)
        span.set_attribute("agent.tool.user_id", context.user_id)
        if context.agent_role:
            span.set_attribute("agent.tool.agent_role", context.agent_role)
        if context.policy_version:
            span.set_attribute("agent.tool.policy_version", context.policy_version)
        if context.session_id:
            span.set_attribute("agent.tool.session_id", context.session_id)

    @staticmethod
    def _set_span_result(span, result: ToolResult, error: Optional[BaseException] = None) -> None:
        span.set_attribute("agent.tool.status", result.status)
        span.set_attribute("agent.tool.attempts", result.attempts)
        span.set_attribute("agent.tool.duration_ms", result.duration_ms)
        span.set_attribute("agent.tool.error_code", result.error_code or "")
        if result.ok:
            span.set_status(Status(StatusCode.OK))
        else:
            if error is not None:
                span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, result.error_message))

    def _finalize(self, result: ToolResult) -> ToolResult:
        from observability.metrics import record_tool_call

        metric_tool = result.tool_name if self.registry.get(result.tool_name) else "unknown"
        record_tool_call(metric_tool, result.ok, result.duration_ms / 1000.0)
        self.recent_events.append(result.summary())
        return result
