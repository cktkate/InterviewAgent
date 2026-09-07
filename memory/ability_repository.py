"""Persistent, weighted ability evidence shared by interviews and practice."""
from __future__ import annotations

import hashlib
import math
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from config import settings
from memory.models import AbilityEvidence, Base, KnowledgeMastery
from memory.time_utils import utc_now
from orchestration.adaptive_policy import score_answer


GLOBAL_SCOPE = "global"
PRACTICE_MAX_SHARE = 0.25
PRACTICE_EVIDENCE_WEIGHT = 0.25
PRACTICE_SELF_RATING_CONFIDENCE = 0.75
PRACTICE_OBSERVATIONS = {
    "correct": 0.90,
    "partial": 0.55,
    "unknown": 0.10,
}


def normalize_scope(target_id: str | None) -> str:
    return str(target_id or "").strip() or GLOBAL_SCOPE


def normalize_knowledge_key(knowledge_point: str) -> str:
    value = " ".join(str(knowledge_point or "general").strip().lower().split())
    return value[:160] or "general"


def difficulty_score(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return {
        "basic": 0.30,
        "easy": 0.30,
        "medium": 0.55,
        "hard": 0.80,
    }.get(str(value or "medium").lower(), 0.55)


def _formal_weight(kind: str, observation: float, expected: float) -> float:
    if kind in {"clarify", "deepen"}:
        return 0.65
    if kind == "challenge" and observation < expected:
        return 0.25
    return 1.0


def _profile_dict(row: KnowledgeMastery) -> Dict[str, Any]:
    formal_mass = float(row.formal_evidence_mass or 0.0)
    practice_mass = float(row.practice_evidence_mass or 0.0)
    return {
        "alpha": round(float(row.formal_alpha or 1.0), 6),
        "beta": round(float(row.formal_beta or 1.0), 6),
        "mastery": round(float(row.mastery or 0.5), 6),
        "raw_mastery": round(float(row.formal_mastery or 0.5), 6),
        "formal_mastery": round(float(row.formal_mastery or 0.5), 6),
        "practice_mastery": round(float(row.practice_mastery or 0.5), 6),
        "confidence": round(float(row.confidence or 0.0), 6),
        "attempts": int(row.formal_attempts or 0) + int(row.practice_attempts or 0),
        "formal_attempts": int(row.formal_attempts or 0),
        "practice_attempts": int(row.practice_attempts or 0),
        "evidence_mass": round(formal_mass + practice_mass, 6),
        "formal_evidence_mass": round(formal_mass, 6),
        "practice_evidence_mass": round(practice_mass, 6),
        "practice_share": round(float(row.practice_share or 0.0), 6),
        "last_difficulty": round(float(row.last_difficulty or 0.55), 6),
        "last_evidence_kind": str(row.last_evidence_kind or ""),
    }


def _recalculate_combined(row: KnowledgeMastery) -> None:
    formal_mass = max(0.0, float(row.formal_evidence_mass or 0.0))
    practice_mass = max(0.0, float(row.practice_evidence_mass or 0.0))
    # The self-rated channel can never own more than one quarter of the prior.
    practice_share = min(
        PRACTICE_MAX_SHARE,
        practice_mass / (formal_mass + practice_mass + 1.0),
    )
    formal_base = float(row.formal_mastery if row.formal_attempts else 0.5)
    practice_base = float(row.practice_mastery if row.practice_attempts else 0.5)
    row.practice_share = practice_share
    row.mastery = (1.0 - practice_share) * formal_base + practice_share * practice_base

    formal_confidence = 1.0 - (1.0 / (1.0 + formal_mass))
    practice_confidence = 1.0 - (1.0 / (1.0 + practice_mass))
    row.confidence = min(
        1.0,
        formal_confidence
        + (1.0 - formal_confidence) * practice_share * practice_confidence,
    )


def apply_evidence_in_session(
    session: Session,
    *,
    evidence_id: str,
    user_id: str,
    target_scope: str,
    knowledge_point: str,
    source_type: str,
    evidence_kind: str,
    observation: float,
    difficulty: float,
    confidence: float,
    session_id: str = "",
    question_id: str = "",
    question: str = "",
    answer: str = "",
    details: Optional[Mapping[str, Any]] = None,
    created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Append one idempotent event and update its aggregate in one transaction."""
    existing_event = session.get(AbilityEvidence, evidence_id)
    knowledge_key = normalize_knowledge_key(knowledge_point)
    scope = normalize_scope(target_scope)
    row = session.execute(
        select(KnowledgeMastery).where(
            KnowledgeMastery.user_id == user_id,
            KnowledgeMastery.target_scope == scope,
            KnowledgeMastery.knowledge_key == knowledge_key,
        )
    ).scalar_one_or_none()

    if existing_event is not None:
        return _profile_dict(row) if row is not None else {}

    if row is None:
        row = KnowledgeMastery(
            id=str(uuid.uuid4()),
            user_id=user_id,
            target_scope=scope,
            knowledge_key=knowledge_key,
            knowledge_point=str(knowledge_point or "general")[:160],
        )
        session.add(row)
        session.flush()

    observation = max(0.0, min(1.0, float(observation)))
    difficulty = max(0.0, min(1.0, float(difficulty)))
    confidence = max(0.1, min(1.0, float(confidence)))
    is_practice = source_type == "practice"

    if is_practice:
        mastery_before = float(row.practice_mastery or 0.5)
        attempts = int(row.practice_attempts or 0) + 1
        weight = PRACTICE_EVIDENCE_WEIGHT
    else:
        mastery_before = float(row.formal_mastery or 0.5)
        attempts = int(row.formal_attempts or 0) + 1
        expected_before = 1.0 / (
            1.0 + math.exp(-4.0 * (mastery_before - difficulty))
        )
        weight = _formal_weight(evidence_kind, observation, expected_before)

    expected = 1.0 / (1.0 + math.exp(-4.0 * (mastery_before - difficulty)))
    effective_weight = confidence * weight
    learning_rate = 0.45 / math.sqrt(attempts)
    mastery_after = max(
        0.0,
        min(
            1.0,
            mastery_before + learning_rate * effective_weight * (observation - expected),
        ),
    )

    if is_practice:
        row.practice_attempts = attempts
        row.practice_mastery = mastery_after
        row.practice_alpha = float(row.practice_alpha or 1.0) + observation * effective_weight
        row.practice_beta = float(row.practice_beta or 1.0) + (1.0 - observation) * effective_weight
        row.practice_evidence_mass = float(row.practice_evidence_mass or 0.0) + effective_weight
    else:
        row.formal_attempts = attempts
        row.formal_mastery = mastery_after
        row.formal_alpha = float(row.formal_alpha or 1.0) + observation * effective_weight
        row.formal_beta = float(row.formal_beta or 1.0) + (1.0 - observation) * effective_weight
        row.formal_evidence_mass = float(row.formal_evidence_mass or 0.0) + effective_weight

    now = created_at or utc_now()
    row.last_difficulty = difficulty
    row.last_evidence_kind = evidence_kind
    row.last_seen_at = now
    _recalculate_combined(row)

    session.add(AbilityEvidence(
        id=evidence_id,
        user_id=user_id,
        target_scope=scope,
        knowledge_point=str(knowledge_point or "general")[:160],
        source_type=source_type,
        evidence_kind=evidence_kind,
        observation=observation,
        difficulty=difficulty,
        confidence=confidence,
        effective_weight=effective_weight,
        session_id=session_id or None,
        question_id=question_id or None,
        question=question,
        answer=answer,
        details_json=dict(details or {}),
        created_at=now,
    ))
    session.flush()
    return _profile_dict(row)


class AbilityRepository:
    def __init__(self) -> None:
        self._engine = None
        self._SessionLocal = None
        self._ready_lock = threading.Lock()

    def _ensure_ready(self) -> None:
        if self._SessionLocal is not None:
            return
        with self._ready_lock:
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
                tables=[KnowledgeMastery.__table__, AbilityEvidence.__table__],
            )

    def load_profile(self, user_id: str, target_id: str = "") -> Dict[str, Dict[str, Any]]:
        self._ensure_ready()
        scope = normalize_scope(target_id)
        scopes = [GLOBAL_SCOPE] if scope == GLOBAL_SCOPE else [GLOBAL_SCOPE, scope]
        with self._SessionLocal() as session:
            rows = list(session.scalars(
                select(KnowledgeMastery).where(
                    KnowledgeMastery.user_id == user_id,
                    KnowledgeMastery.target_scope.in_(scopes),
                )
            ).all())

        # Global practice is a useful prior; target-specific formal evidence wins
        # for the same normalized knowledge point.
        rows.sort(key=lambda item: item.target_scope != GLOBAL_SCOPE)
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            result[row.knowledge_point] = _profile_dict(row)
        return result

    def record_interview_evidence(
        self,
        state: Mapping[str, Any],
        record_index: int,
    ) -> Dict[str, Any]:
        records = list(state.get("score_records") or [])
        if record_index < 0 or record_index >= len(records):
            return {}
        record = records[record_index]
        score = dict(record.get("score") or {})
        user_id = str(state.get("user_id") or "")
        session_id = str(state.get("session_id") or "")
        if not user_id or not session_id:
            return {}

        question_id = str(record.get("question_id") or "")
        kind = str(record.get("asked_followup_type") or "primary")
        stable_key = f"interview:{session_id}:{record_index}:{question_id}:{kind}"
        evidence_id = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()
        knowledge_point = str(record.get("knowledge_point") or "general")
        confidence = float(score.get("evaluation_confidence", 0.7))
        self._ensure_ready()
        with self._SessionLocal() as session:
            profile = apply_evidence_in_session(
                session,
                evidence_id=evidence_id,
                user_id=user_id,
                target_scope=normalize_scope(str(state.get("target_id") or "")),
                knowledge_point=knowledge_point,
                source_type="interview",
                evidence_kind=kind,
                observation=score_answer(score),
                difficulty=difficulty_score(record.get("difficulty_score", 0.55)),
                confidence=confidence,
                session_id=session_id,
                question_id=question_id,
                question=str(record.get("question") or ""),
                answer=str(record.get("answer") or ""),
                details={"score": score, "is_anchor": bool(record.get("is_anchor"))},
            )
            session.commit()
            return profile


ability_repository = AbilityRepository()
