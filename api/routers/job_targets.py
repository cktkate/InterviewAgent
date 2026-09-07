"""求职目标 CRUD API。"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.schemas import JobTargetCreate, JobTargetResponse, JobTargetUpdate
from api.security import get_current_user_id
from memory.job_target_repository import job_target_repository

router = APIRouter(prefix="/job-targets", tags=["求职目标"])


@router.post("", response_model=JobTargetResponse, status_code=status.HTTP_201_CREATED)
def create_job_target(
    req: JobTargetCreate,
    user_id: str = Depends(get_current_user_id),
):
    return job_target_repository.create(user_id, req.model_dump())


@router.get("", response_model=List[JobTargetResponse])
def list_job_targets(
    include_archived: bool = Query(False),
    user_id: str = Depends(get_current_user_id),
):
    return job_target_repository.list_for_user(user_id, include_archived)


@router.get("/{target_id}", response_model=JobTargetResponse)
def get_job_target(
    target_id: str,
    user_id: str = Depends(get_current_user_id),
):
    target = job_target_repository.get_for_user(target_id, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="求职目标不存在")
    return target


@router.patch("/{target_id}", response_model=JobTargetResponse)
def update_job_target(
    target_id: str,
    req: JobTargetUpdate,
    user_id: str = Depends(get_current_user_id),
):
    changes = req.model_dump(exclude_unset=True)
    target = job_target_repository.update(target_id, user_id, changes)
    if target is None:
        raise HTTPException(status_code=404, detail="求职目标不存在")
    return target


@router.post("/{target_id}/archive", response_model=JobTargetResponse)
def archive_job_target(
    target_id: str,
    user_id: str = Depends(get_current_user_id),
):
    target = job_target_repository.archive(target_id, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="求职目标不存在")
    return target
