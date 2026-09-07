"""SQLAlchemy ORM 模型定义（长期记忆）"""
from __future__ import annotations

from sqlalchemy import Boolean, JSON, Column, Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase

from memory.time_utils import utc_now


class Base(DeclarativeBase):
    pass


class UserAccount(Base):
    """Login account. Passwords are stored as salted hashes only."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), unique=True, index=True, nullable=False)
    username = Column(String(32), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    last_login_at = Column(DateTime, nullable=True)


class UserProfile(Base):
    """用户画像（长期记忆核心）"""
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)
    total_interviews = Column(Integer, default=0)
    average_score = Column(Integer, default=0)
    persistent_weaknesses = Column(JSON, default=list)  # 持久化薄弱点
    tech_strengths = Column(JSON, default=list)          # 技术优势


class InterviewRecord(Base):
    """面试历史记录"""
    __tablename__ = "interview_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), index=True, nullable=False)
    session_id = Column(String(64), unique=True, nullable=False)
    interview_date = Column(DateTime, default=utc_now)
    jd_title = Column(String(200))
    overall_score = Column(Integer)
    recommendation = Column(String(32))
    report_json = Column(JSON)
    study_plan_json = Column(JSON)
    weaknesses = Column(JSON, default=list)


class InterviewCheckpoint(Base):
    """面试进度检查点。

    Redis 仍是实时会话的首选存储；该表只用于 Redis 数据不可用时的灾难恢复。
    """

    __tablename__ = "interview_checkpoints"

    session_id = Column(String(64), primary_key=True)
    state_json = Column(JSON, nullable=False)
    updated_at = Column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )


class BackgroundTask(Base):
    """Durable lifecycle record for an asynchronous business task."""

    __tablename__ = "background_tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_background_task_idempotency"),
    )

    task_id = Column(String(64), primary_key=True)
    idempotency_key = Column(String(160), nullable=False)
    task_type = Column(String(64), index=True, nullable=False)
    session_id = Column(String(64), index=True, nullable=False)
    user_id = Column(String(64), index=True, nullable=False)
    status = Column(String(24), index=True, nullable=False)
    stage = Column(String(64), default="queued", nullable=False)
    progress = Column(Integer, default=0, nullable=False)
    attempt = Column(Integer, default=0, nullable=False)
    trace_id = Column(String(64), default="", nullable=False)
    error_type = Column(String(120), default="", nullable=False)
    error_message = Column(String(500), default="", nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )
    completed_at = Column(DateTime, nullable=True)


class KnowledgeMastery(Base):
    """Cross-session mastery for one user, target scope and knowledge point.

    Formal interview evidence and self-rated practice evidence are tracked in
    separate channels.  The derived ``mastery`` value caps the practice channel
    at 25%, so self-rating can guide future question selection without changing
    a formal interview score.
    """

    __tablename__ = "knowledge_mastery"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "target_scope", "knowledge_key",
            name="uq_mastery_user_scope_knowledge",
        ),
    )

    id = Column(String(36), primary_key=True)
    user_id = Column(String(64), index=True, nullable=False)
    target_scope = Column(String(64), default="global", index=True, nullable=False)
    knowledge_key = Column(String(160), index=True, nullable=False)
    knowledge_point = Column(String(160), nullable=False)

    formal_alpha = Column(Float, default=1.0, nullable=False)
    formal_beta = Column(Float, default=1.0, nullable=False)
    formal_mastery = Column(Float, default=0.5, nullable=False)
    formal_evidence_mass = Column(Float, default=0.0, nullable=False)
    formal_attempts = Column(Integer, default=0, nullable=False)

    practice_alpha = Column(Float, default=1.0, nullable=False)
    practice_beta = Column(Float, default=1.0, nullable=False)
    practice_mastery = Column(Float, default=0.5, nullable=False)
    practice_evidence_mass = Column(Float, default=0.0, nullable=False)
    practice_attempts = Column(Integer, default=0, nullable=False)

    mastery = Column(Float, default=0.5, nullable=False)
    confidence = Column(Float, default=0.0, nullable=False)
    practice_share = Column(Float, default=0.0, nullable=False)
    last_difficulty = Column(Float, default=0.55, nullable=False)
    last_evidence_kind = Column(String(32), default="", nullable=False)
    last_seen_at = Column(DateTime, default=utc_now, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )


class AbilityEvidence(Base):
    """Append-only, idempotent evidence used to build ``KnowledgeMastery``."""

    __tablename__ = "ability_evidence"

    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), index=True, nullable=False)
    target_scope = Column(String(64), index=True, nullable=False)
    knowledge_point = Column(String(160), index=True, nullable=False)
    source_type = Column(String(24), index=True, nullable=False)  # interview/practice
    evidence_kind = Column(String(32), nullable=False)
    observation = Column(Float, nullable=False)
    difficulty = Column(Float, default=0.55, nullable=False)
    confidence = Column(Float, nullable=False)
    effective_weight = Column(Float, nullable=False)
    session_id = Column(String(64), index=True, nullable=True)
    question_id = Column(String(64), index=True, nullable=True)
    question = Column(Text, default="", nullable=False)
    answer = Column(Text, default="", nullable=False)
    details_json = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=utc_now, index=True, nullable=False)


class PracticeQuestionScope(Base):
    """Target mapping for interview-derived practice questions.

    Kept in a separate table so existing practice tables do not require an
    in-place schema alteration when this feature is deployed.
    """

    __tablename__ = "practice_question_scopes"

    question_id = Column(String(36), primary_key=True)
    user_id = Column(String(64), index=True, nullable=False)
    target_scope = Column(String(64), default="global", index=True, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)


class JobTarget(Base):
    """求职者的长期求职目标。与单次面试解耦，可关联多次训练。"""

    __tablename__ = "job_targets"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(64), index=True, nullable=False)
    title = Column(String(120), nullable=False)
    company_name = Column(String(120), default="", nullable=False)
    position_name = Column(String(120), nullable=False)
    jd_text = Column(Text, default="", nullable=False)
    target_date = Column(Date, nullable=True)
    status = Column(String(20), default="preparing", index=True, nullable=False)
    focus_areas = Column(JSON, default=list, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )


class JobTargetSession(Base):
    """用独立关联表连接旧面试会话，避免破坏已有表结构。"""

    __tablename__ = "job_target_sessions"

    session_id = Column(String(64), primary_key=True)
    target_id = Column(String(36), index=True, nullable=False)
    user_id = Column(String(64), index=True, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)


class LearningPlan(Base):
    """围绕一个求职目标持续迭代的学习计划。"""

    __tablename__ = "learning_plans"

    id = Column(String(36), primary_key=True)
    target_id = Column(String(36), unique=True, index=True, nullable=False)
    user_id = Column(String(64), index=True, nullable=False)
    target_date = Column(Date, nullable=False)
    daily_minutes = Column(Integer, default=60, nullable=False)
    days_per_week = Column(Integer, default=6, nullable=False)
    status = Column(String(20), default="active", nullable=False)
    version = Column(Integer, default=1, nullable=False)
    generated_reason = Column(String(255), default="initial", nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )


class LearningPhase(Base):
    __tablename__ = "learning_phases"

    id = Column(String(36), primary_key=True)
    plan_id = Column(String(36), index=True, nullable=False)
    title = Column(String(120), nullable=False)
    objective = Column(Text, default="", nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    sequence = Column(Integer, nullable=False)
    version = Column(Integer, default=1, nullable=False)


class LearningTask(Base):
    __tablename__ = "learning_tasks"

    id = Column(String(36), primary_key=True)
    phase_id = Column(String(36), index=True, nullable=False)
    plan_id = Column(String(36), index=True, nullable=False)
    title = Column(String(160), nullable=False)
    description = Column(Text, default="", nullable=False)
    knowledge_topic = Column(String(120), default="", nullable=False)
    planned_date = Column(Date, nullable=False)
    estimated_minutes = Column(Integer, default=30, nullable=False)
    status = Column(String(20), default="pending", index=True, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    user_note = Column(Text, default="", nullable=False)
    sequence = Column(Integer, nullable=False)
    version = Column(Integer, default=1, nullable=False)


class LearningTaskResource(Base):
    """由真实工具检索并绑定到学习任务的外部资源。"""

    __tablename__ = "learning_task_resources"

    id = Column(String(36), primary_key=True)
    task_id = Column(String(36), index=True, nullable=False)
    plan_id = Column(String(36), index=True, nullable=False)
    title = Column(String(255), nullable=False)
    url = Column(Text, nullable=False)
    source_type = Column(String(32), default="github", nullable=False)
    description = Column(Text, default="", nullable=False)
    stars = Column(Integer, default=0, nullable=False)
    verified_at = Column(DateTime, default=utc_now, nullable=False)


class KnowledgeNote(Base):
    """知识点笔记（用户标注/收藏的面试知识点）"""
    __tablename__ = "knowledge_notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), index=True, nullable=False)
    topic = Column(String(200), nullable=False)
    content = Column(Text)
    created_at = Column(DateTime, default=utc_now)


class PracticeQuestion(Base):
    """专项背诵题库。

    ``owner_user_id`` 为空的是内置公共题；正式面试产生的复习题只对所属用户可见，
    避免包含个人 JD/简历信息的题目泄露给其他账户。
    """

    __tablename__ = "practice_questions"

    id = Column(String(36), primary_key=True)
    owner_user_id = Column(String(64), nullable=True, index=True)
    fingerprint = Column(String(64), nullable=False, index=True)
    stem = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    tech_stack = Column(String(64), default="general", index=True, nullable=False)
    knowledge_point = Column(String(120), default="未分类", index=True, nullable=False)
    difficulty = Column(String(16), default="medium", nullable=False)
    keywords = Column(JSON, default=list, nullable=False)
    origin = Column(String(24), default="builtin", index=True, nullable=False)
    source_session_id = Column(String(64), nullable=True, index=True)
    is_active = Column(Boolean, default=True, index=True, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class PracticeAttempt(Base):
    """用户对照答案后的自评记录；只追加，不覆盖历史。"""

    __tablename__ = "practice_attempts"

    id = Column(String(36), primary_key=True)
    practice_session_id = Column(String(36), index=True, nullable=False)
    user_id = Column(String(64), index=True, nullable=False)
    question_id = Column(String(36), index=True, nullable=False)
    rating = Column(String(16), index=True, nullable=False)  # correct / partial / unknown
    spoken_answer = Column(Text, default="", nullable=False)
    source = Column(String(24), default="practice", nullable=False)
    created_at = Column(DateTime, default=utc_now, index=True, nullable=False)


class PracticeReviewItem(Base):
    """统一复习队列：正式面试弱点与专项背诵自评共享。"""

    __tablename__ = "practice_review_items"
    __table_args__ = (
        UniqueConstraint("user_id", "question_id", name="uq_practice_review_user_question"),
    )

    id = Column(String(36), primary_key=True)
    user_id = Column(String(64), index=True, nullable=False)
    question_id = Column(String(36), index=True, nullable=False)
    source = Column(String(24), default="practice", index=True, nullable=False)
    source_session_id = Column(String(64), nullable=True, index=True)
    level = Column(Integer, default=0, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    correct_streak = Column(Integer, default=0, nullable=False)
    last_rating = Column(String(16), default="", nullable=False)
    status = Column(String(16), default="active", index=True, nullable=False)
    due_at = Column(DateTime, default=utc_now, index=True, nullable=False)
    last_reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)
