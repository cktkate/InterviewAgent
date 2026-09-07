"""Redis Streams event bus shared by Celery workers and WebSocket processes."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional

from loguru import logger

from config import settings


_sync_client = None
_client_lock = threading.Lock()


def _stream_key(session_id: str) -> str:
    return f"{settings.task_event_stream_prefix}:{session_id}"


def _transport() -> str:
    value = str(settings.task_event_transport or "pubsub").strip().lower()
    return value if value in {"pubsub", "streams"} else "pubsub"


def _get_sync_client():
    global _sync_client
    if _sync_client is not None:
        return _sync_client
    with _client_lock:
        if _sync_client is None:
            import redis

            _sync_client = redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                password=settings.redis_password or None,
                db=settings.task_event_redis_db,
                decode_responses=True,
                protocol=2,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
            )
        return _sync_client


def publish_task_event(
    *,
    session_id: str,
    task_id: str,
    status: str,
    stage: str,
    progress: int,
    attempt: int = 0,
    trace_id: str = "",
    message: str = "",
) -> Optional[str]:
    """Append operational progress only; report content never enters Redis Streams."""
    if not settings.task_event_stream_enabled or not session_id:
        return None
    fields = {
        "task_id": task_id,
        "status": status,
        "stage": stage,
        "progress": str(max(0, min(100, int(progress)))),
        "attempt": str(max(0, int(attempt))),
        "trace_id": trace_id,
        "message": message[:200],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        client = _get_sync_client()
        if _transport() == "streams":
            return str(client.xadd(
                _stream_key(session_id),
                fields,
                maxlen=max(10, settings.task_event_stream_maxlen),
                approximate=True,
            ))
        import json

        return str(client.publish(
            _stream_key(session_id), json.dumps(fields, ensure_ascii=False)
        ))
    except Exception as exc:
        logger.warning("任务进度事件发布失败，保留MySQL/会话状态降级: {}", exc)
        return None


async def stream_task_events(
    session_id: str, *, last_id: str = "$"
) -> AsyncIterator[Dict[str, Any]]:
    """Continuously read new session events and reconnect after Redis failures."""
    if not settings.task_event_stream_enabled:
        return
    cursor = last_id
    while True:
        client = None
        pubsub = None
        try:
            import redis.asyncio as redis_async

            client = redis_async.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                password=settings.redis_password or None,
                db=settings.task_event_redis_db,
                decode_responses=True,
                protocol=2,
                socket_connect_timeout=3,
                socket_timeout=max(3, settings.task_event_stream_block_ms / 1000 + 2),
                health_check_interval=30,
            )
            if _transport() == "pubsub":
                import json

                pubsub = client.pubsub()
                await pubsub.subscribe(_stream_key(session_id))
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    fields = json.loads(message.get("data") or "{}")
                    yield _normalize_event(fields, "")
            else:
                while True:
                    rows = await client.xread(
                        {_stream_key(session_id): cursor},
                        count=20,
                        block=max(100, settings.task_event_stream_block_ms),
                    )
                    for _, events in rows:
                        for event_id, fields in events:
                            cursor = str(event_id)
                            yield _normalize_event(fields, cursor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("任务事件订阅中断，稍后重连: {}", exc)
            await asyncio.sleep(2)
        finally:
            if pubsub is not None:
                await pubsub.aclose()
            if client is not None:
                await client.aclose()


def _normalize_event(fields: Dict[str, Any], event_id: str) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "task_id": str(fields.get("task_id") or ""),
        "status": str(fields.get("status") or ""),
        "stage": str(fields.get("stage") or ""),
        "progress": int(fields.get("progress") or 0),
        "attempt": int(fields.get("attempt") or 0),
        "trace_id": str(fields.get("trace_id") or ""),
        "message": str(fields.get("message") or ""),
        "timestamp": str(fields.get("timestamp") or ""),
    }
