"""Cross-session ability memory and interview/practice weighting tests."""
from datetime import datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from memory.ability_repository import (
    AbilityRepository,
    PRACTICE_MAX_SHARE,
    apply_evidence_in_session,
)
from memory.models import AbilityEvidence, KnowledgeMastery


def _repository() -> AbilityRepository:
    repository = AbilityRepository()
    repository._engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repository._SessionLocal = sessionmaker(
        bind=repository._engine, expire_on_commit=False
    )
    KnowledgeMastery.__table__.create(repository._engine)
    AbilityEvidence.__table__.create(repository._engine)
    return repository


def _interview_state(target_id: str = "target-1"):
    return {
        "user_id": "user-1",
        "session_id": "interview-1",
        "target_id": target_id,
        "score_records": [{
            "question_id": "Q1",
            "question": "Explain RRF",
            "answer": "Fuse ranked lists by reciprocal rank.",
            "knowledge_point": "RRF",
            "difficulty_score": 0.55,
            "asked_followup_type": "primary",
            "is_anchor": True,
            "score": {
                "correctness": 9,
                "depth": 8,
                "structure": 8,
                "example": 7,
                "evaluation_confidence": 0.9,
            },
        }],
    }


def test_interview_evidence_is_append_only_and_idempotent():
    repository = _repository()
    first = repository.record_interview_evidence(_interview_state(), 0)
    second = repository.record_interview_evidence(_interview_state(), 0)

    assert first == second
    assert first["formal_attempts"] == 1
    assert first["practice_attempts"] == 0
    assert first["mastery"] > 0.5
    with repository._SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AbilityEvidence)) == 1


def test_practice_is_a_weak_prior_capped_at_twenty_five_percent():
    repository = _repository()
    with repository._SessionLocal() as session:
        for index in range(50):
            result = apply_evidence_in_session(
                session,
                evidence_id=f"practice-{index}",
                user_id="user-1",
                target_scope="target-1",
                knowledge_point="RRF",
                source_type="practice",
                evidence_kind="practice_correct",
                observation=0.9,
                difficulty=0.55,
                confidence=0.75,
                session_id="practice-1",
                question_id="P1",
                created_at=datetime(2026, 8, 21, 9, 0, 0),
            )
        session.commit()

    assert result["practice_share"] <= PRACTICE_MAX_SHARE
    assert result["formal_attempts"] == 0
    assert result["practice_attempts"] == 50
    # Even repeated self-ratings cannot turn a neutral formal prior into a
    # near-perfect formal ability estimate.
    assert result["mastery"] < 0.63


def test_target_profiles_are_isolated_and_global_is_a_fallback():
    repository = _repository()
    repository.record_interview_evidence(_interview_state("target-a"), 0)
    state_b = _interview_state("target-b")
    state_b["session_id"] = "interview-2"
    state_b["score_records"][0]["knowledge_point"] = "MySQL"
    repository.record_interview_evidence(state_b, 0)

    profile_a = repository.load_profile("user-1", "target-a")
    profile_b = repository.load_profile("user-1", "target-b")
    assert "RRF" in profile_a and "MySQL" not in profile_a
    assert "MySQL" in profile_b and "RRF" not in profile_b
    assert repository.load_profile("user-2", "target-a") == {}
