"""MySQL repository for candidate job targets."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from config import settings
from memory.models import Base, JobTarget, JobTargetSession


class JobTargetRepository:
    def __init__(self) -> None:
        self._engine = None
        self._SessionLocal = None

    def _ensure_ready(self) -> None:
        if self._SessionLocal is not None:
            return
        self._engine = create_engine(
            settings.mysql_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        self._SessionLocal = sessionmaker(bind=self._engine, expire_on_commit=False)
        Base.metadata.create_all(
            self._engine,
            tables=[JobTarget.__table__, JobTargetSession.__table__],
        )

    @staticmethod
    def _as_dict(target: JobTarget) -> Dict[str, Any]:
        return {
            "id": target.id,
            "user_id": target.user_id,
            "title": target.title,
            "company_name": target.company_name or "",
            "position_name": target.position_name,
            "jd_text": target.jd_text or "",
            "target_date": target.target_date,
            "status": target.status,
            "focus_areas": list(target.focus_areas or []),
            "created_at": target.created_at,
            "updated_at": target.updated_at,
        }

    def create(self, user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_ready()
        target = JobTarget(
            id=str(uuid.uuid4()),
            user_id=user_id,
            title=data["title"],
            company_name=data.get("company_name", ""),
            position_name=data["position_name"],
            jd_text=data.get("jd_text", ""),
            target_date=data.get("target_date"),
            status=data.get("status", "preparing"),
            focus_areas=data.get("focus_areas", []),
        )
        with self._SessionLocal() as session:
            session.add(target)
            session.commit()
            session.refresh(target)
            return self._as_dict(target)

    def list_for_user(self, user_id: str, include_archived: bool = False) -> List[Dict[str, Any]]:
        self._ensure_ready()
        statement = select(JobTarget).where(JobTarget.user_id == user_id)
        if not include_archived:
            statement = statement.where(JobTarget.status != "archived")
        statement = statement.order_by(JobTarget.updated_at.desc())
        with self._SessionLocal() as session:
            return [self._as_dict(item) for item in session.scalars(statement).all()]

    def get_for_user(self, target_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        self._ensure_ready()
        with self._SessionLocal() as session:
            target = session.execute(
                select(JobTarget).where(
                    JobTarget.id == target_id,
                    JobTarget.user_id == user_id,
                )
            ).scalar_one_or_none()
            return self._as_dict(target) if target else None

    def update(self, target_id: str, user_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self._ensure_ready()
        with self._SessionLocal() as session:
            target = session.execute(
                select(JobTarget).where(
                    JobTarget.id == target_id,
                    JobTarget.user_id == user_id,
                )
            ).scalar_one_or_none()
            if target is None:
                return None
            for field, value in changes.items():
                setattr(target, field, value)
            session.commit()
            session.refresh(target)
            return self._as_dict(target)

    def archive(self, target_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        return self.update(target_id, user_id, {"status": "archived"})

    def link_session(self, target_id: str, session_id: str, user_id: str) -> bool:
        self._ensure_ready()
        with self._SessionLocal() as session:
            existing = session.get(JobTargetSession, session_id)
            if existing is None:
                session.add(JobTargetSession(
                    session_id=session_id,
                    target_id=target_id,
                    user_id=user_id,
                ))
            else:
                existing.target_id = target_id
                existing.user_id = user_id
            session.commit()
        return True


job_target_repository = JobTargetRepository()
