"""Durable and idempotent background-task lifecycle storage."""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Tuple

from loguru import logger
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from config import settings
from memory.models import BackgroundTask, Base
from memory.time_utils import utc_now


class BackgroundTaskRepository:
    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = database_url
        self._engine = None
        self._SessionLocal = None
        self._lock = threading.Lock()
        self._fallback: Dict[str, Dict[str, Any]] = {}
        self._idempotency_index: Dict[str, str] = {}

    def _ensure_ready(self) -> bool:
        if self._SessionLocal is not None:
            return True
        try:
            self._engine = create_engine(
                self.database_url or settings.mysql_url,
                pool_pre_ping=True,
                **({} if (self.database_url or "").startswith("sqlite") else {
                    "pool_size": 5,
                    "max_overflow": 10,
                }),
            )
            self._SessionLocal = sessionmaker(
                bind=self._engine, expire_on_commit=False
            )
            Base.metadata.create_all(
                self._engine, tables=[BackgroundTask.__table__]
            )
            return True
        except Exception as exc:
            logger.warning("后台任务MySQL不可用，状态降级到进程内存: {}", exc)
            self._engine = None
            self._SessionLocal = None
            return False

    @staticmethod
    def _as_dict(record: BackgroundTask) -> Dict[str, Any]:
        return {
            "task_id": record.task_id,
            "idempotency_key": record.idempotency_key,
            "task_type": record.task_type,
            "session_id": record.session_id,
            "user_id": record.user_id,
            "status": record.status,
            "stage": record.stage,
            "progress": int(record.progress or 0),
            "attempt": int(record.attempt or 0),
            "trace_id": record.trace_id or "",
            "error_type": record.error_type or "",
            "error_message": record.error_message or "",
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "completed_at": record.completed_at,
        }

    def create_or_get(
        self,
        *,
        task_id: str,
        idempotency_key: str,
        task_type: str,
        session_id: str,
        user_id: str,
        trace_id: str = "",
    ) -> Tuple[Dict[str, Any], bool]:
        if not self._ensure_ready():
            with self._lock:
                existing_id = self._idempotency_index.get(idempotency_key)
                if existing_id:
                    return dict(self._fallback[existing_id]), False
                now = utc_now()
                value = {
                    "task_id": task_id,
                    "idempotency_key": idempotency_key,
                    "task_type": task_type,
                    "session_id": session_id,
                    "user_id": user_id,
                    "status": "queued",
                    "stage": "queued",
                    "progress": 5,
                    "attempt": 0,
                    "trace_id": trace_id,
                    "error_type": "",
                    "error_message": "",
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                }
                self._fallback[task_id] = value
                self._idempotency_index[idempotency_key] = task_id
                return dict(value), True

        with self._SessionLocal() as session:
            existing = session.scalar(
                select(BackgroundTask).where(
                    BackgroundTask.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                return self._as_dict(existing), False
            record = BackgroundTask(
                task_id=task_id,
                idempotency_key=idempotency_key,
                task_type=task_type,
                session_id=session_id,
                user_id=user_id,
                status="queued",
                stage="queued",
                progress=5,
                attempt=0,
                trace_id=trace_id,
            )
            session.add(record)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(BackgroundTask).where(
                        BackgroundTask.idempotency_key == idempotency_key
                    )
                )
                if existing is None:
                    raise
                return self._as_dict(existing), False
            return self._as_dict(record), True

    def update(
        self,
        task_id: str,
        *,
        status: str,
        stage: str,
        progress: int,
        attempt: Optional[int] = None,
        trace_id: Optional[str] = None,
        error_type: str = "",
        error_message: str = "",
    ) -> Dict[str, Any]:
        progress = max(0, min(100, int(progress)))
        if not self._ensure_ready():
            with self._lock:
                value = self._fallback.get(task_id)
                if value is None:
                    return {}
                value.update({
                    "status": status,
                    "stage": stage,
                    "progress": progress,
                    "error_type": error_type,
                    "error_message": error_message[:500],
                    "updated_at": utc_now(),
                })
                if attempt is not None:
                    value["attempt"] = int(attempt)
                if trace_id is not None:
                    value["trace_id"] = trace_id
                if status == "completed":
                    value["completed_at"] = utc_now()
                return dict(value)

        with self._SessionLocal() as session:
            record = session.get(BackgroundTask, task_id)
            if record is None:
                return {}
            record.status = status
            record.stage = stage
            record.progress = progress
            record.error_type = error_type
            record.error_message = error_message[:500]
            record.updated_at = utc_now()
            if attempt is not None:
                record.attempt = int(attempt)
            if trace_id is not None:
                record.trace_id = trace_id
            if status == "completed":
                record.completed_at = utc_now()
            session.commit()
            return self._as_dict(record)

    def get(self, task_id: str) -> Dict[str, Any]:
        if not task_id:
            return {}
        if not self._ensure_ready():
            with self._lock:
                return dict(self._fallback.get(task_id) or {})
        with self._SessionLocal() as session:
            record = session.get(BackgroundTask, task_id)
            return self._as_dict(record) if record else {}


background_task_repository = BackgroundTaskRepository()
