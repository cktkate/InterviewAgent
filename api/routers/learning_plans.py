"""Deadline-aware learning plan APIs for candidates."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from starlette.concurrency import run_in_threadpool

from agents.learning_plan import (
    LearningPlanAgent,
    enrich_phases_with_resources,
    schedule_draft,
)
from api.schemas import (
    LearningPlanGenerateRequest,
    LearningPlanReplanRequest,
    LearningPlanResponse,
    LearningTaskResponse,
    LearningTaskUpdate,
)
from api.security import get_current_user_id
from memory.job_target_repository import job_target_repository
from memory.learning_plan_repository import learning_plan_repository
from memory.memory_manager import memory_manager

router = APIRouter(prefix="/learning-plans", tags=["学习计划"])


def _target_or_404(target_id: str, user_id: str):
    target = job_target_repository.get_for_user(target_id, user_id)
    if target is None or target["status"] == "archived":
        raise HTTPException(status_code=404, detail="求职目标不存在或已归档")
    if target["target_date"] is None:
        raise HTTPException(status_code=422, detail="请先为求职目标设置目标日期")
    if target["target_date"] <= date.today():
        raise HTTPException(status_code=422, detail="目标日期必须晚于今天")
    return target


def _generate(
    *,
    target,
    user_id: str,
    daily_minutes: int,
    days_per_week: int,
    reason: str,
    previous_plan,
):
    completed = []
    unfinished = []
    if previous_plan:
        for phase in previous_plan["phases"]:
            for task in phase["tasks"]:
                item = {"title": task["title"], "topic": task["knowledge_topic"]}
                if task["status"] == "completed":
                    completed.append(item)
                else:
                    unfinished.append(item)
    planning_context = {
        "position": target["position_name"],
        "company": target["company_name"],
        "jd": target["jd_text"][:6000],
        "focus_areas": target["focus_areas"],
        "target_date": target["target_date"],
        "days_remaining": (target["target_date"] - date.today()).days,
        "daily_minutes": daily_minutes,
        "days_per_week": days_per_week,
        "historical_weaknesses": memory_manager.get_weaknesses(user_id),
        "completed_tasks_do_not_repeat": completed,
        "unfinished_tasks": unfinished,
        "replan_reason": reason,
    }
    draft = LearningPlanAgent().run(planning_context)["learning_plan_draft"]
    phases = schedule_draft(
        draft,
        start_date=date.today(),
        target_date=target["target_date"],
    )
    phases = enrich_phases_with_resources(phases)
    return learning_plan_repository.save_generated(
        target_id=target["id"],
        user_id=user_id,
        target_date=target["target_date"],
        daily_minutes=daily_minutes,
        days_per_week=days_per_week,
        reason=reason,
        phases=phases,
        replace_future=previous_plan is not None,
    )


@router.post("/generate", response_model=LearningPlanResponse)
async def generate_learning_plan(
    req: LearningPlanGenerateRequest,
    user_id: str = Depends(get_current_user_id),
):
    target = _target_or_404(req.target_id, user_id)
    existing = learning_plan_repository.get_for_target(req.target_id, user_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail="该目标已有学习计划，请使用重新规划")
    try:
        return await run_in_threadpool(
            _generate,
            target=target,
            user_id=user_id,
            daily_minutes=req.daily_minutes,
            days_per_week=req.days_per_week,
            reason="initial",
            previous_plan=None,
        )
    except Exception as exc:
        logger.exception("生成学习计划失败")
        raise HTTPException(status_code=502, detail=f"生成学习计划失败：{exc}")


@router.get("/by-target/{target_id}", response_model=LearningPlanResponse)
def get_learning_plan(
    target_id: str,
    user_id: str = Depends(get_current_user_id),
):
    _target_or_404(target_id, user_id)
    plan = learning_plan_repository.get_for_target(target_id, user_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="该目标还没有学习计划")
    return plan


@router.post("/by-target/{target_id}/replan", response_model=LearningPlanResponse)
async def replan_learning_plan(
    target_id: str,
    req: LearningPlanReplanRequest,
    user_id: str = Depends(get_current_user_id),
):
    target = _target_or_404(target_id, user_id)
    previous = learning_plan_repository.get_for_target(target_id, user_id)
    if previous is None:
        raise HTTPException(status_code=404, detail="该目标还没有学习计划")
    try:
        return await run_in_threadpool(
            _generate,
            target=target,
            user_id=user_id,
            daily_minutes=req.daily_minutes or previous["daily_minutes"],
            days_per_week=req.days_per_week or previous["days_per_week"],
            reason=req.reason,
            previous_plan=previous,
        )
    except Exception as exc:
        logger.exception("重新规划学习计划失败")
        raise HTTPException(status_code=502, detail=f"重新规划失败：{exc}")


@router.patch("/tasks/{task_id}", response_model=LearningTaskResponse)
def update_learning_task(
    task_id: str,
    req: LearningTaskUpdate,
    user_id: str = Depends(get_current_user_id),
):
    if req.status is None and req.user_note is None:
        raise HTTPException(status_code=422, detail="至少更新状态或备注之一")
    task = learning_plan_repository.update_task(
        task_id,
        user_id,
        status=req.status,
        user_note=req.user_note,
    )
    if task is None:
        raise HTTPException(status_code=404, detail="学习任务不存在")
    return task
