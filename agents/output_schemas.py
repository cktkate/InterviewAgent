"""Strict schemas for LLM outputs used by routing and evaluation."""
from __future__ import annotations

import json
import re
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictOutputModel(BaseModel):
    """Reject unexpected fields so prompt/schema drift is visible."""

    model_config = ConfigDict(extra="forbid")


class IntentResult(StrictOutputModel):
    intent: Literal[
        "start_interview",
        "answer_question",
        "upload_resume",
        "input_jd",
        "use_skill",
        "chat",
        "unknown",
    ]
    confidence: float = Field(ge=0, le=1)
    skill: Optional[Literal["quiz", "teach", "project", "compare"]] = None

    @model_validator(mode="after")
    def validate_skill_selection(self) -> "IntentResult":
        if self.intent == "use_skill" and self.skill is None:
            raise ValueError("use_skill intent requires a skill")
        if self.intent != "use_skill" and self.skill is not None:
            raise ValueError("skill must be null unless intent is use_skill")
        return self


class SingleScoreResult(StrictOutputModel):
    correctness: float = Field(ge=0, le=10)
    depth: float = Field(ge=0, le=10)
    structure: float = Field(ge=0, le=10)
    example: float = Field(ge=0, le=10)
    is_correct: bool
    key_missing: List[str] = Field(default_factory=list, max_length=3)
    followup_needed: bool
    followup_reason: str = ""
    followup_type: Literal["none", "clarify", "deepen", "challenge"] = "none"
    evaluation_confidence: float = Field(default=0.7, ge=0, le=1)

    @field_validator("key_missing", mode="before")
    @classmethod
    def normalize_key_missing(cls, value):
        """Keep useful omissions without retrying when the model returns extras."""
        if value is None:
            return []
        if isinstance(value, str):
            value = re.split(r"(?:\r?\n)+|[；;、]+", value)
        if isinstance(value, (list, tuple)):
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            return cleaned[:3]
        return value

    @model_validator(mode="after")
    def validate_business_rules(self) -> "SingleScoreResult":
        expected_correct = self.correctness >= 6 and self.depth >= 5
        if self.is_correct != expected_correct:
            raise ValueError(
                "is_correct must equal (correctness >= 6 and depth >= 5)"
            )
        if self.followup_needed and not self.followup_reason.strip():
            raise ValueError("followup_needed=true requires followup_reason")
        if not self.followup_needed and self.followup_type != "none":
            raise ValueError("followup_type must be none when followup is not needed")
        return self


class JDAnalysisResult(StrictOutputModel):
    title: str
    level: Literal["junior", "mid", "senior", "staff", "principal"]
    years_of_experience: int = Field(ge=0, le=50)
    tech_stack: List[str]
    nice_to_have: List[str]
    core_competencies: List[str]
    industry: str
    key_responsibilities: List[str] = Field(min_length=1, max_length=8)
    summary: str


class TechMatch(StrictOutputModel):
    matched: bool
    evidence: str


class ResumeAnalysisResult(StrictOutputModel):
    overall_score: int = Field(ge=0, le=100)
    match_level: Literal["low", "medium", "high"]
    strengths: List[str]
    weaknesses: List[str]
    tech_match: Dict[str, TechMatch]
    experience_fit: Literal["under", "fit", "over"]
    highlights: List[str]
    red_flags: List[str]
    summary: str


class QuestionItem(StrictOutputModel):
    topic: str
    question_type: Literal["concept", "scenario", "coding", "behavioral", "project"]
    difficulty: Literal["easy", "medium", "hard"]
    focus: str
    prompt: str
    knowledge_point: str = "general"
    difficulty_score: float = Field(default=0.55, ge=0, le=1)
    expected_points: List[str] = Field(default_factory=list, max_length=8)
    prerequisites: List[str] = Field(default_factory=list, max_length=8)
    job_weight: float = Field(default=0.5, ge=0, le=1)
    discrimination: float = Field(default=0.5, ge=0, le=1)
    estimated_minutes: int = Field(default=4, ge=1, le=30)
    is_anchor: bool = False

    @field_validator("expected_points", "prerequisites", mode="before")
    @classmethod
    def coerce_generated_list(cls, value):
        """Accept common LLM list-like strings without triggering a full retry."""
        if value is None:
            return []
        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text or text.lower() in {"none", "null", "n/a", "无", "暂无"}:
            return []

        # Some models serialize the JSON array as a quoted string.
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass

        parts = re.split(r"(?:\r?\n)+|[；;、]+", text)
        cleaned = []
        for part in parts:
            item = re.sub(r"^\s*(?:[-*•·]|\d+[.)、])\s*", "", part).strip()
            if item:
                cleaned.append(item)

        # Preserve all generated information while respecting schema max_length.
        if len(cleaned) > 8:
            cleaned = cleaned[:7] + ["；".join(cleaned[7:])]
        return cleaned


class QuestionPlanResult(StrictOutputModel):
    plan: List[QuestionItem] = Field(min_length=1)


class QuestionDraft(StrictOutputModel):
    """Compact LLM contract; policy-only numeric fields are derived in code."""
    topic: str
    question_type: Literal["concept", "scenario", "coding", "behavioral", "project"]
    difficulty: Literal["easy", "medium", "hard"]
    focus: str
    prompt: str
    knowledge_point: str = "general"
    expected_points: List[str] = Field(default_factory=list, max_length=6)

    @field_validator("expected_points", mode="before")
    @classmethod
    def coerce_expected_points(cls, value):
        return QuestionItem.coerce_generated_list(value)


class PlanningEvidenceRequest(StrictOutputModel):
    """A bounded, read-only evidence action proposed by the planning LLM."""

    action: Literal["rag_retrieve"]
    query: str = Field(min_length=2, max_length=300)
    reason: str = Field(min_length=2, max_length=300)
    required: bool = False


class QuestionDraftPlan(StrictOutputModel):
    plan: List[QuestionDraft] = Field(min_length=1)
    evidence_requests: List[PlanningEvidenceRequest] = Field(
        default_factory=list, max_length=2
    )


class RerankResult(StrictOutputModel):
    order: List[str] = Field(min_length=1, max_length=20)
    relevance_scores: Dict[str, float] = Field(default_factory=dict)


class InterviewQuestionResult(StrictOutputModel):
    question: str = Field(min_length=1, max_length=500)
    is_followup: bool


class NormalQuestionResult(InterviewQuestionResult):
    is_followup: Literal[False]


class FollowupQuestionResult(InterviewQuestionResult):
    is_followup: Literal[True]


class InterviewCommentResult(StrictOutputModel):
    comment: str = Field(min_length=1, max_length=500)


class InterviewFarewellResult(StrictOutputModel):
    farewell: str = Field(min_length=1, max_length=500)


class DimensionScores(StrictOutputModel):
    technical_knowledge: float = Field(ge=0, le=10)
    problem_solving: float = Field(ge=0, le=10)
    system_design: float = Field(ge=0, le=10)
    communication: float = Field(ge=0, le=10)
    practical_experience: float = Field(ge=0, le=10)
    learning_ability: float = Field(ge=0, le=10)


class TopicPerformance(StrictOutputModel):
    topic: str
    performance: Literal["good", "average", "weak"]
    comment: str


class FinalReportResult(StrictOutputModel):
    overall_score: int = Field(ge=0, le=100)
    recommendation: Literal["strong_hire", "hire", "weak_hire", "no_hire"]
    dimension_scores: DimensionScores
    strengths: List[str]
    weaknesses: List[str]
    highlights: List[str]
    concerns: List[str]
    topic_performance: List[TopicPerformance]
    summary: str
    interviewer_comment: str


class StudyWeek(StrictOutputModel):
    week: int = Field(ge=1, le=52)
    theme: str
    goals: str
    daily_hours: float = Field(gt=0, le=24)
    resources: str


class StudyPlanResult(StrictOutputModel):
    overall_advice: str
    weeks: List[StudyWeek] = Field(min_length=1)
    practice_projects: List[str]
    mock_interview_tips: List[str]


class LearningTaskDraft(StrictOutputModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    knowledge_topic: str = Field(min_length=1, max_length=120)
    estimated_minutes: int = Field(ge=10, le=240)


class LearningPhaseDraft(StrictOutputModel):
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=1000)
    duration_days: int = Field(ge=1, le=90)
    tasks: List[LearningTaskDraft] = Field(min_length=1, max_length=20)


class LearningPlanDraft(StrictOutputModel):
    overall_strategy: str = Field(min_length=1, max_length=1000)
    phases: List[LearningPhaseDraft] = Field(min_length=1, max_length=8)
