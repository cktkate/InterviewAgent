"""
Pydantic 请求/响应数据模型
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


# ── 请求模型 ──────────────────────────────────────────────────────────────────

class StartInterviewRequest(BaseModel):
    jd_text:         str            = Field("", description="岗位 JD 文本")
    target_id:       Optional[str]  = Field(None, description="可选求职目标 ID")
    interview_topic: str            = Field("", description="Explicit interview topic")
    resume_text:     str            = Field("",  description="简历文本（可选）")
    total_questions: int            = Field(5,   ge=1, le=20, description="出题数量")
    voice:           Optional[str]  = Field(None, description="TTS 音色")


class AnswerRequest(BaseModel):
    session_id:  str = Field(..., description="会话 ID")
    answer:      str = Field(..., description="用户回答文本")


class SkillRequest(BaseModel):
    session_id:  str = Field(..., description="会话 ID")
    skill_name:  str = Field(..., description="技能名称: quiz/teach/project/compare")
    user_input:  str = Field("",  description="技能输入内容")


# ── 响应模型 ──────────────────────────────────────────────────────────────────

class StartInterviewResponse(BaseModel):
    session_id:           str
    target_id:            Optional[str] = None
    question:             str
    question_index:       int
    total_questions:      int
    difficulty:           str
    jd_title:             str = ""


class AnswerResponse(BaseModel):
    session_id:           str
    interview_finished:   bool
    should_followup:      bool
    question:             str            = ""   # 下一题或追问
    question_index:       int            = 0
    total_questions:      int            = 0
    difficulty:           str            = "medium"
    final_report:         Optional[Dict[str, Any]] = None
    study_plan:           Optional[Dict[str, Any]] = None
    github_recommendations: List[Dict[str, Any]]   = []


class ReportResponse(BaseModel):
    session_id:             str
    target_id:              Optional[str] = None
    final_report:           Dict[str, Any]
    study_plan:             Dict[str, Any]
    github_recommendations: List[Dict[str, Any]] = []


class HealthResponse(BaseModel):
    status:  str = "ok"
    version: str = "1.0.0"


class UploadResponse(BaseModel):
    filename:    str
    text:        str
    char_count:  int


class ErrorResponse(BaseModel):
    error:   str
    detail:  str = ""


# ── 求职目标 ──

JobTargetStatus = Literal["preparing", "interviewing", "completed", "archived"]


class JobTargetCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    company_name: str = Field("", max_length=120)
    position_name: str = Field(min_length=1, max_length=120)
    jd_text: str = Field("", max_length=50000)
    target_date: Optional[date] = None
    status: JobTargetStatus = "preparing"
    focus_areas: List[str] = Field(default_factory=list, max_length=20)


class JobTargetUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=120)
    company_name: Optional[str] = Field(None, max_length=120)
    position_name: Optional[str] = Field(None, min_length=1, max_length=120)
    jd_text: Optional[str] = Field(None, max_length=50000)
    target_date: Optional[date] = None
    status: Optional[JobTargetStatus] = None
    focus_areas: Optional[List[str]] = Field(None, max_length=20)


class JobTargetResponse(BaseModel):
    id: str
    title: str
    company_name: str
    position_name: str
    jd_text: str
    target_date: Optional[date]
    status: JobTargetStatus
    focus_areas: List[str]
    created_at: datetime
    updated_at: datetime


# ── 可执行学习计划 ──

LearningTaskStatus = Literal["pending", "in_progress", "completed", "skipped"]


class LearningPlanGenerateRequest(BaseModel):
    target_id: str
    daily_minutes: int = Field(60, ge=15, le=480)
    days_per_week: int = Field(6, ge=1, le=7)


class LearningPlanReplanRequest(BaseModel):
    reason: str = Field("用户主动重新规划", min_length=1, max_length=255)
    daily_minutes: Optional[int] = Field(None, ge=15, le=480)
    days_per_week: Optional[int] = Field(None, ge=1, le=7)


class LearningTaskUpdate(BaseModel):
    status: Optional[LearningTaskStatus] = None
    user_note: Optional[str] = Field(None, max_length=2000)


class LearningResourceResponse(BaseModel):
    id: str
    title: str
    url: str
    source_type: str
    description: str
    stars: int
    verified_at: datetime


class LearningTaskResponse(BaseModel):
    id: str
    phase_id: str
    title: str
    description: str
    knowledge_topic: str
    planned_date: date
    estimated_minutes: int
    status: LearningTaskStatus
    completed_at: Optional[datetime]
    user_note: str
    sequence: int
    resources: List[LearningResourceResponse]


class LearningPhaseResponse(BaseModel):
    id: str
    title: str
    objective: str
    start_date: date
    end_date: date
    sequence: int
    version: int
    tasks: List[LearningTaskResponse]


class LearningPlanResponse(BaseModel):
    id: str
    target_id: str
    target_date: date
    daily_minutes: int
    days_per_week: int
    status: str
    version: int
    generated_reason: str
    created_at: datetime
    updated_at: datetime
    completed_tasks: int
    total_tasks: int
    progress_percent: int
    phases: List[LearningPhaseResponse]


# ── 八股专项练习 ──

PracticeRating = Literal["correct", "partial", "unknown"]


class PracticeAttemptCreate(BaseModel):
    question_id: str = Field(min_length=1, max_length=36)
    practice_session_id: str = Field(min_length=1, max_length=36)
    rating: PracticeRating
    spoken_answer: str = Field("", max_length=10000)
    source: Literal["practice", "review", "interview"] = "practice"


class PracticeQuestionResponse(BaseModel):
    id: str
    stem: str
    tech_stack: str
    knowledge_point: str
    difficulty: str
    keywords: List[str] = Field(default_factory=list)
    origin: str


class PracticeAnswerResponse(PracticeQuestionResponse):
    answer: str
