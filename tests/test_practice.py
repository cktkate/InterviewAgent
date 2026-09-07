"""专项背诵题库、自评调度与正式面试错题回流测试。"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory.models import (
    AbilityEvidence,
    KnowledgeMastery,
    PracticeAttempt,
    PracticeQuestion,
    PracticeQuestionScope,
    PracticeReviewItem,
)
from memory.practice_repository import PracticeRepository


def _repository() -> PracticeRepository:
    repository = PracticeRepository()
    repository._engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repository._SessionLocal = sessionmaker(
        bind=repository._engine, expire_on_commit=False
    )
    PracticeQuestion.__table__.create(repository._engine)
    PracticeAttempt.__table__.create(repository._engine)
    PracticeReviewItem.__table__.create(repository._engine)
    PracticeQuestionScope.__table__.create(repository._engine)
    KnowledgeMastery.__table__.create(repository._engine)
    AbilityEvidence.__table__.create(repository._engine)
    return repository


def test_builtin_catalog_and_answer_are_separate():
    repository = _repository()
    catalog = repository.catalog("user-1")
    assert catalog["total_questions"] >= 10
    assert {item["name"] for item in catalog["tech_stacks"]} >= {"agent", "rag", "python"}

    question = repository.next_question("user-1", tech_stack="rag")
    assert question is not None
    assert question["tech_stack"] == "rag"
    assert "answer" not in question

    revealed = repository.reveal_answer("user-1", question["id"])
    assert revealed is not None
    assert revealed["answer"]


def test_self_rating_uses_deterministic_review_schedule():
    repository = _repository()
    question = repository.next_question("user-1", tech_stack="python")
    assert question is not None
    now = datetime(2026, 8, 20, 9, 0, 0)

    correct = repository.record_attempt(
        "user-1", question["id"], "session-1", "correct", now=now
    )
    assert correct["level"] == 1
    assert correct["due_at"] == now + timedelta(days=1)

    partial = repository.record_attempt(
        "user-1", question["id"], "session-1", "partial", now=now
    )
    assert partial["level"] == 1
    assert partial["due_at"] == now + timedelta(days=1)

    unknown = repository.record_attempt(
        "user-1", question["id"], "session-1", "unknown", now=now
    )
    assert unknown["level"] == 0
    assert unknown["due_at"] == now + timedelta(minutes=10)
    assert unknown["attempts"] == 3


def test_due_mode_only_returns_due_items():
    repository = _repository()
    question = repository.next_question("user-1", tech_stack="agent")
    now = datetime(2026, 8, 20, 9, 0, 0)
    repository.record_attempt(
        "user-1", question["id"], "session-1", "correct", now=now
    )
    assert repository.next_question("user-1", mode="due", now=now) is None
    due = repository.next_question(
        "user-1", mode="due", now=now + timedelta(days=1, seconds=1)
    )
    assert due["id"] == question["id"]


def test_formal_interview_weakness_is_private_and_idempotent():
    repository = _repository()
    state = {
        "user_id": "user-1",
        "session_id": "interview-1",
        "question_plan": [{
            "id": "Q1",
            "prompt": "解释 Agent 中的工具权限控制",
            "topic": "agent",
            "knowledge_point": "工具安全",
            "difficulty": "medium",
            "expected_points": ["最小权限", "参数校验", "高风险操作确认"],
        }],
        "score_records": [{
            "question_id": "Q1",
            "question": "解释 Agent 中的工具权限控制",
            "knowledge_point": "工具安全",
            "is_followup": False,
            "score": {"is_correct": False, "key_missing": ["最小权限"]},
        }],
    }
    now = datetime(2026, 8, 20, 9, 0, 0)
    assert repository.enqueue_interview_weaknesses(state, now=now) == 1
    assert repository.enqueue_interview_weaknesses(state, now=now) == 0

    item = repository.next_question("user-1", mode="interview", now=now)
    assert item is not None
    assert item["origin"] == "interview"
    assert repository.next_question("user-2", mode="interview", now=now) is None

    with repository._SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(PracticeReviewItem)) == 1
        assert session.scalar(select(func.count()).select_from(PracticeQuestion).where(
            PracticeQuestion.origin == "interview"
        )) == 1


def test_correct_interview_answer_is_not_enqueued():
    repository = _repository()
    state = {
        "user_id": "user-1",
        "session_id": "interview-1",
        "question_plan": [{"id": "Q1", "expected_points": ["正确要点"]}],
        "score_records": [{
            "question_id": "Q1", "question": "已经掌握的问题",
            "is_followup": False, "score": {"is_correct": True},
        }],
    }
    assert repository.enqueue_interview_weaknesses(state) == 0


def test_practice_http_flow_hides_answer_until_reveal(monkeypatch):
    from api.routers import practice as practice_router
    from api.security import get_current_user_id

    repository = _repository()
    monkeypatch.setattr(practice_router, "practice_repository", repository)
    app = FastAPI()
    app.include_router(practice_router.router)
    app.dependency_overrides[get_current_user_id] = lambda: "user-1"
    client = TestClient(app)

    next_response = client.get("/practice/questions/next", params={"tech_stack": "rag"})
    assert next_response.status_code == 200
    question = next_response.json()
    assert "answer" not in question

    answer_response = client.get(f"/practice/questions/{question['id']}/answer")
    assert answer_response.status_code == 200
    assert answer_response.json()["answer"]

    attempt_response = client.post("/practice/attempts", json={
        "question_id": question["id"],
        "practice_session_id": "session-1",
        "rating": "partial",
        "spoken_answer": "我的口述答案",
        "source": "practice",
    })
    assert attempt_response.status_code == 200
    assert attempt_response.json()["review"]["last_rating"] == "partial"
