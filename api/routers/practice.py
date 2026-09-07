"""八股专项练习 API：题库抽题、答案揭示、自评和复习调度。"""
from __future__ import annotations

from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from api.schemas import PracticeAnswerResponse, PracticeAttemptCreate, PracticeQuestionResponse
from api.security import get_current_user_id
from memory.practice_repository import practice_repository


router = APIRouter(prefix="/practice", tags=["八股专项练习"])


@router.get("/overview")
def practice_overview(user_id: str = Depends(get_current_user_id)):
    return practice_repository.overview(user_id)


@router.get("/catalog")
def practice_catalog(user_id: str = Depends(get_current_user_id)):
    return practice_repository.catalog(user_id)


@router.get("/questions/next", response_model=PracticeQuestionResponse | None)
def next_practice_question(
    mode: Literal["category", "due", "interview"] = Query("category"),
    tech_stack: str = Query("", max_length=64),
    knowledge_point: str = Query("", max_length=120),
    exclude_id: List[str] = Query(default=[]),
    user_id: str = Depends(get_current_user_id),
):
    return practice_repository.next_question(
        user_id=user_id,
        mode=mode,
        tech_stack=tech_stack.strip(),
        knowledge_point=knowledge_point.strip(),
        exclude_ids=exclude_id[-30:],
    )


@router.get("/questions/{question_id}/answer", response_model=PracticeAnswerResponse)
def reveal_practice_answer(
    question_id: str,
    user_id: str = Depends(get_current_user_id),
):
    question = practice_repository.reveal_answer(user_id, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="专项练习题不存在")
    return question


@router.post("/attempts")
def create_practice_attempt(
    req: PracticeAttemptCreate,
    user_id: str = Depends(get_current_user_id),
):
    review = practice_repository.record_attempt(
        user_id=user_id,
        question_id=req.question_id,
        practice_session_id=req.practice_session_id,
        rating=req.rating,
        spoken_answer=req.spoken_answer,
        source=req.source,
    )
    if review is None:
        raise HTTPException(status_code=404, detail="专项练习题不存在")
    return {"recorded": True, "review": review}
