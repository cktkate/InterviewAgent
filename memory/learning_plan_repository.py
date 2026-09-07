"""Persistence for actionable, versioned learning plans."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from config import settings
from memory.models import (
    Base,
    LearningPhase,
    LearningPlan,
    LearningTask,
    LearningTaskResource,
)
from memory.time_utils import utc_now


class LearningPlanRepository:
    def __init__(self) -> None:
        self._engine = None
        self._SessionLocal = None

    def _ensure_ready(self) -> None:
        if self._SessionLocal is not None:
            return
        self._engine = create_engine(
            settings.mysql_url, pool_pre_ping=True, pool_size=5, max_overflow=10
        )
        self._SessionLocal = sessionmaker(bind=self._engine, expire_on_commit=False)
        Base.metadata.create_all(
            self._engine,
            tables=[
                LearningPlan.__table__,
                LearningPhase.__table__,
                LearningTask.__table__,
                LearningTaskResource.__table__,
            ],
        )

    @staticmethod
    def _resource_dict(resource: LearningTaskResource) -> Dict[str, Any]:
        return {
            "id": resource.id,
            "title": resource.title,
            "url": resource.url,
            "source_type": resource.source_type,
            "description": resource.description or "",
            "stars": resource.stars or 0,
            "verified_at": resource.verified_at,
        }

    def _task_dict(self, task: LearningTask, session=None) -> Dict[str, Any]:
        resources = []
        if session is not None:
            resources = [
                self._resource_dict(item)
                for item in session.scalars(
                    select(LearningTaskResource)
                    .where(LearningTaskResource.task_id == task.id)
                    .order_by(LearningTaskResource.stars.desc())
                ).all()
            ]
        return {
            "id": task.id,
            "phase_id": task.phase_id,
            "title": task.title,
            "description": task.description or "",
            "knowledge_topic": task.knowledge_topic or "",
            "planned_date": task.planned_date,
            "estimated_minutes": task.estimated_minutes,
            "status": task.status,
            "completed_at": task.completed_at,
            "user_note": task.user_note or "",
            "sequence": task.sequence,
            "resources": resources,
        }

    def _nested(self, session, plan: LearningPlan) -> Dict[str, Any]:
        phases = session.scalars(
            select(LearningPhase)
            .where(LearningPhase.plan_id == plan.id)
            .order_by(LearningPhase.sequence)
        ).all()
        phase_items = []
        completed = 0
        total = 0
        for phase in phases:
            tasks = session.scalars(
                select(LearningTask)
                .where(LearningTask.phase_id == phase.id)
                .order_by(LearningTask.sequence)
            ).all()
            task_items = [self._task_dict(task, session) for task in tasks]
            total += len(task_items)
            completed += sum(item["status"] == "completed" for item in task_items)
            phase_items.append({
                "id": phase.id,
                "title": phase.title,
                "objective": phase.objective,
                "start_date": phase.start_date,
                "end_date": phase.end_date,
                "sequence": phase.sequence,
                "version": phase.version,
                "tasks": task_items,
            })
        return {
            "id": plan.id,
            "target_id": plan.target_id,
            "target_date": plan.target_date,
            "daily_minutes": plan.daily_minutes,
            "days_per_week": plan.days_per_week,
            "status": plan.status,
            "version": plan.version,
            "generated_reason": plan.generated_reason,
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
            "completed_tasks": completed,
            "total_tasks": total,
            "progress_percent": round(completed * 100 / total) if total else 0,
            "phases": phase_items,
        }

    def get_for_target(self, target_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        self._ensure_ready()
        with self._SessionLocal() as session:
            plan = session.execute(
                select(LearningPlan).where(
                    LearningPlan.target_id == target_id,
                    LearningPlan.user_id == user_id,
                )
            ).scalar_one_or_none()
            return self._nested(session, plan) if plan else None

    def save_generated(
        self,
        *,
        target_id: str,
        user_id: str,
        target_date,
        daily_minutes: int,
        days_per_week: int,
        reason: str,
        phases: List[Dict[str, Any]],
        replace_future: bool,
    ) -> Dict[str, Any]:
        self._ensure_ready()
        with self._SessionLocal() as session:
            plan = session.execute(
                select(LearningPlan).where(
                    LearningPlan.target_id == target_id,
                    LearningPlan.user_id == user_id,
                )
            ).scalar_one_or_none()
            if plan is None:
                plan = LearningPlan(
                    id=str(uuid.uuid4()),
                    target_id=target_id,
                    user_id=user_id,
                    target_date=target_date,
                    daily_minutes=daily_minutes,
                    days_per_week=days_per_week,
                    generated_reason=reason,
                )
                session.add(plan)
                session.flush()
            else:
                plan.target_date = target_date
                plan.daily_minutes = daily_minutes
                plan.days_per_week = days_per_week
                plan.generated_reason = reason
                plan.updated_at = utc_now()
                if replace_future:
                    # 已完成任务是用户历史，不允许模型覆盖。
                    removable_task_ids = select(LearningTask.id).where(
                        LearningTask.plan_id == plan.id,
                        LearningTask.status != "completed",
                    )
                    session.execute(
                        delete(LearningTaskResource).where(
                            LearningTaskResource.task_id.in_(removable_task_ids)
                        )
                    )
                    session.execute(
                        delete(LearningTask).where(
                            LearningTask.plan_id == plan.id,
                            LearningTask.status != "completed",
                        )
                    )
                    completed_phase_ids = select(LearningTask.phase_id).where(
                        LearningTask.plan_id == plan.id,
                        LearningTask.status == "completed",
                    )
                    session.execute(
                        delete(LearningPhase).where(
                            LearningPhase.plan_id == plan.id,
                            ~LearningPhase.id.in_(completed_phase_ids),
                        )
                    )
                    plan.version += 1
                else:
                    raise ValueError("learning plan already exists")

            sequence_offset = session.scalar(
                select(LearningPhase.sequence)
                .where(LearningPhase.plan_id == plan.id)
                .order_by(LearningPhase.sequence.desc())
                .limit(1)
            ) or 0
            for phase_index, phase_data in enumerate(phases, start=1):
                phase = LearningPhase(
                    id=str(uuid.uuid4()),
                    plan_id=plan.id,
                    title=phase_data["title"],
                    objective=phase_data["objective"],
                    start_date=phase_data["start_date"],
                    end_date=phase_data["end_date"],
                    sequence=sequence_offset + phase_index,
                    version=plan.version,
                )
                session.add(phase)
                session.flush()
                for task_index, task_data in enumerate(phase_data["tasks"], start=1):
                    task = LearningTask(
                        id=str(uuid.uuid4()),
                        phase_id=phase.id,
                        plan_id=plan.id,
                        title=task_data["title"],
                        description=task_data.get("description", ""),
                        knowledge_topic=task_data.get("knowledge_topic", ""),
                        planned_date=task_data["planned_date"],
                        estimated_minutes=task_data["estimated_minutes"],
                        sequence=task_index,
                        version=plan.version,
                    )
                    session.add(task)
                    session.flush()
                    for resource in task_data.get("resources", []):
                        if not resource.get("url"):
                            continue
                        session.add(LearningTaskResource(
                            id=str(uuid.uuid4()),
                            task_id=task.id,
                            plan_id=plan.id,
                            title=resource.get("title") or resource["url"],
                            url=resource["url"],
                            source_type=resource.get("source_type", "github"),
                            description=resource.get("description", ""),
                            stars=int(resource.get("stars") or 0),
                        ))
            session.commit()
            session.refresh(plan)
            return self._nested(session, plan)

    def update_task(
        self,
        task_id: str,
        user_id: str,
        *,
        status: Optional[str] = None,
        user_note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        self._ensure_ready()
        with self._SessionLocal() as session:
            task = session.execute(
                select(LearningTask)
                .join(LearningPlan, LearningPlan.id == LearningTask.plan_id)
                .where(LearningTask.id == task_id, LearningPlan.user_id == user_id)
            ).scalar_one_or_none()
            if task is None:
                return None
            if status is not None:
                task.status = status
                task.completed_at = utc_now() if status == "completed" else None
            if user_note is not None:
                task.user_note = user_note
            session.commit()
            session.refresh(task)
            return self._task_dict(task, session)


learning_plan_repository = LearningPlanRepository()
