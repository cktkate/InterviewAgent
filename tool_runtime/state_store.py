"""Shared reliability state for Tool Runtime.

The Redis implementation keeps rate limits, circuit breakers and idempotency
results consistent across API workers. Raw arguments and idempotency keys are
never stored; keys are SHA-256 digests and values contain only tool results.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any, Mapping, Optional, Protocol

from config import settings


class ToolStateStore(Protocol):
    def check_limits(
        self,
        tool_name: str,
        *,
        rate_limit_per_minute: int,
    ) -> Optional[tuple[str, str]]: ...

    def record_success(self, tool_name: str) -> None: ...

    def record_failure(
        self,
        tool_name: str,
        *,
        threshold: int,
        open_seconds: float,
    ) -> None: ...

    def get_idempotency(self, tool_name: str, key: str) -> Optional[dict[str, Any]]: ...

    def set_idempotency(
        self, tool_name: str, key: str, result: Mapping[str, Any]
    ) -> None: ...


_LIMIT_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return {'circuit_open', 'tool circuit breaker is open'}
end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
local count = redis.call('ZCARD', KEYS[2])
if count >= tonumber(ARGV[2]) then
  return {'rate_limited', 'tool rate limit exceeded'}
end
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[4])
redis.call('EXPIRE', KEYS[2], 61)
return {'ok', ''}
"""

_FAILURE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
if count >= tonumber(ARGV[2]) then
  redis.call('SET', KEYS[2], '1', 'EX', ARGV[3])
end
return count
"""


class RedisToolStateStore:
    def __init__(self) -> None:
        import redis

        self._client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            db=settings.tool_runtime_redis_db,
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=3,
            socket_timeout=3,
            health_check_interval=30,
        )
        self._client.ping()
        self._prefix = settings.tool_runtime_state_prefix.strip(":")

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _tool_key(self, kind: str, tool_name: str) -> str:
        return f"{self._prefix}:{kind}:{self._digest(tool_name)}"

    def _idempotency_key(self, tool_name: str, key: str) -> str:
        return f"{self._prefix}:idempotency:{self._digest(tool_name + ':' + key)}"

    def check_limits(
        self,
        tool_name: str,
        *,
        rate_limit_per_minute: int,
    ) -> Optional[tuple[str, str]]:
        now = time.time()
        result = self._client.eval(
            _LIMIT_SCRIPT,
            2,
            self._tool_key("circuit", tool_name),
            self._tool_key("rate", tool_name),
            now - 60.0,
            max(1, int(rate_limit_per_minute)),
            now,
            f"{now}:{uuid.uuid4()}",
        )
        code = str(result[0])
        return None if code == "ok" else (code, str(result[1]))

    def record_success(self, tool_name: str) -> None:
        self._client.delete(
            self._tool_key("failures", tool_name),
            self._tool_key("circuit", tool_name),
        )

    def record_failure(
        self,
        tool_name: str,
        *,
        threshold: int,
        open_seconds: float,
    ) -> None:
        ttl = max(60, int(math.ceil(open_seconds)))
        self._client.eval(
            _FAILURE_SCRIPT,
            2,
            self._tool_key("failures", tool_name),
            self._tool_key("circuit", tool_name),
            ttl,
            max(1, int(threshold)),
            max(1, int(math.ceil(open_seconds))),
        )

    def get_idempotency(self, tool_name: str, key: str) -> Optional[dict[str, Any]]:
        raw = self._client.get(self._idempotency_key(tool_name, key))
        return json.loads(raw) if raw else None

    def set_idempotency(
        self, tool_name: str, key: str, result: Mapping[str, Any]
    ) -> None:
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        self._client.set(
            self._idempotency_key(tool_name, key),
            serialized,
            ex=max(1, settings.tool_runtime_idempotency_ttl_seconds),
        )


def create_tool_state_store() -> Optional[ToolStateStore]:
    backend = settings.tool_runtime_state_backend.strip().lower()
    if backend == "memory":
        return None
    if backend == "redis":
        return RedisToolStateStore()
    raise ValueError("TOOL_RUNTIME_STATE_BACKEND must be one of: memory, redis")
