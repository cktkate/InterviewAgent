"""Health checks with bounded latency and a short readiness cache."""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
import redis
from sqlalchemy import create_engine, text

from config import settings
from observability.metrics import DEPENDENCY_READY


@dataclass(frozen=True)
class DependencyCheck:
    status: str
    latency_ms: float
    required: bool = True
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "status": self.status,
            "required": self.required,
            "latency_ms": round(self.latency_ms, 3),
        }
        if self.reason:
            value["reason"] = self.reason
        return value


_cache_lock = asyncio.Lock()
_cached_at = 0.0
_cached_result: dict[str, DependencyCheck] = {}


def _mysql_ping() -> None:
    engine = create_engine(
        settings.mysql_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": max(1, int(settings.health_check_timeout_seconds))},
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def _redis_ping() -> None:
    client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        db=settings.redis_db,
        socket_connect_timeout=settings.health_check_timeout_seconds,
        socket_timeout=settings.health_check_timeout_seconds,
    )
    try:
        if not client.ping():
            raise RuntimeError("ping returned false")
    finally:
        client.close()


async def _weaviate_ping() -> None:
    url = settings.weaviate_url.rstrip("/") + "/v1/.well-known/ready"
    headers = {"Authorization": f"Bearer {settings.weaviate_api_key}"} if settings.weaviate_api_key else {}
    async with httpx.AsyncClient(timeout=settings.health_check_timeout_seconds) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()


def _celery_ping() -> None:
    from tasks.celery_app import celery_app

    replies = celery_app.control.inspect(
        timeout=settings.health_check_timeout_seconds
    ).ping()
    if not replies:
        raise RuntimeError("no Celery worker replied")


async def _run_check(
    name: str,
    check: Callable[[], None] | Callable[[], Awaitable[None]],
    *,
    required: bool,
) -> tuple[str, DependencyCheck]:
    started = time.perf_counter()
    try:
        if inspect.iscoroutinefunction(check):
            await asyncio.wait_for(check(), timeout=settings.health_check_timeout_seconds)
        else:
            await asyncio.wait_for(
                asyncio.to_thread(check),
                timeout=settings.health_check_timeout_seconds,
            )
        status, reason = "up", ""
    except Exception as exc:
        status, reason = "down", type(exc).__name__
    elapsed = (time.perf_counter() - started) * 1000
    DEPENDENCY_READY.labels(dependency=name).set(1 if status == "up" else 0)
    return name, DependencyCheck(status, elapsed, required, reason)


async def check_readiness(*, force: bool = False) -> dict[str, DependencyCheck]:
    global _cached_at, _cached_result
    now = time.monotonic()
    if not force and _cached_result and now - _cached_at < settings.readiness_cache_seconds:
        return dict(_cached_result)
    async with _cache_lock:
        now = time.monotonic()
        if not force and _cached_result and now - _cached_at < settings.readiness_cache_seconds:
            return dict(_cached_result)
        checks = [
            _run_check("mysql", _mysql_ping, required=True),
            _run_check("redis", _redis_ping, required=True),
            _run_check("weaviate", _weaviate_ping, required=True),
        ]
        if settings.celery_enabled:
            checks.append(_run_check("celery", _celery_ping, required=True))
        else:
            DEPENDENCY_READY.labels(dependency="celery").set(1)
        pairs = await asyncio.gather(*checks)
        result = dict(pairs)
        if not settings.celery_enabled:
            result["celery"] = DependencyCheck(
                "disabled", 0.0, False, "CELERY_ENABLED=false"
            )
        _cached_result, _cached_at = result, time.monotonic()
        return dict(result)
