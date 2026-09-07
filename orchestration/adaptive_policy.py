"""Deterministic adaptive interview policy.

LLMs generate and evaluate content.  This module owns the auditable decision
of what to ask next, using only structured state and bounded numeric signals.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import math
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Tuple


DIFFICULTY_SCORE = {"easy": 0.30, "medium": 0.55, "hard": 0.80}


def normalize_question(question: Mapping[str, Any], index: int = 0) -> Dict[str, Any]:
    normalized = dict(question)
    normalized.setdefault("id", f"Q{index + 1}")
    normalized.setdefault("topic", "general")
    normalized.setdefault("knowledge_point", normalized.get("topic", "general"))
    normalized.setdefault("difficulty", "medium")
    normalized.setdefault(
        "difficulty_score",
        DIFFICULTY_SCORE.get(str(normalized["difficulty"]), 0.55),
    )
    normalized.setdefault("expected_points", [])
    normalized.setdefault("prerequisites", [])
    normalized.setdefault("job_weight", 0.5)
    normalized.setdefault("discrimination", 0.5)
    normalized.setdefault("estimated_minutes", 4)
    normalized.setdefault("is_anchor", False)
    return normalized


def initialize_candidate_pool(
    state: MutableMapping[str, Any], questions: Iterable[Mapping[str, Any]]
) -> None:
    candidates = [normalize_question(question, i) for i, question in enumerate(questions)]
    state["question_candidates"] = candidates
    state["question_plan"] = []
    state["ability_profile"] = dict(state.get("ability_profile") or {})
    state["policy_decisions"] = list(state.get("policy_decisions") or [])
    state["completed_primary_questions"] = 0


def score_answer(score: Mapping[str, Any]) -> float:
    """Convert the four evaluator dimensions into a normalized observation."""
    weighted = (
        float(score.get("correctness", 0)) * 0.45
        + float(score.get("depth", 0)) * 0.30
        + float(score.get("structure", 0)) * 0.15
        + float(score.get("example", 0)) * 0.10
    )
    return max(0.0, min(1.0, weighted / 10.0))


def update_ability_profile(
    state: MutableMapping[str, Any],
    score: Mapping[str, Any],
    evidence_kind: str = "primary",
) -> Dict[str, Any]:
    """Update difficulty-calibrated ability from one evaluated answer.

    The update is Elo/IRT-like: evidence is the residual between observed
    performance and expected performance at the question difficulty.  A hard
    question can therefore provide more positive evidence than an easy one.
    Failed challenge follow-ups are deliberately down-weighted because they
    probe the ceiling rather than baseline job competence.
    """
    question = normalize_question(state.get("current_question") or {})
    knowledge_point = str(question.get("knowledge_point") or question.get("topic"))
    profile = state.setdefault("ability_profile", {})
    previous = dict(profile.get(knowledge_point) or {})
    alpha = float(previous.get("alpha", 1.0))
    beta = float(previous.get("beta", 1.0))
    ability_before = float(previous.get("mastery", 0.5))
    difficulty = float(question.get("difficulty_score", 0.55))
    observation = score_answer(score)
    evidence_confidence = float(score.get("evaluation_confidence", 0.7))
    evidence_confidence = max(0.1, min(1.0, evidence_confidence))

    expected = 1.0 / (1.0 + math.exp(-4.0 * (ability_before - difficulty)))
    evidence_weight = 1.0
    if evidence_kind in {"clarify", "deepen"}:
        evidence_weight = 0.65
    elif evidence_kind == "challenge" and observation < expected:
        evidence_weight = 0.25
    effective_confidence = evidence_confidence * evidence_weight

    attempts = int(previous.get("attempts", 0)) + 1
    learning_rate = 0.45 / math.sqrt(attempts)
    residual = observation - expected
    mastery = max(
        0.0,
        min(1.0, ability_before + learning_rate * effective_confidence * residual),
    )

    alpha += observation * effective_confidence
    beta += (1.0 - observation) * effective_confidence
    if "evidence_mass" in previous:
        previous_evidence_mass = float(previous.get("evidence_mass", 0.0))
    else:
        # Recover equivalent evidence mass from pre-upgrade confidence.
        previous_confidence = max(
            0.0, min(0.999, float(previous.get("confidence", 0.0)))
        )
        previous_evidence_mass = previous_confidence / (1.0 - previous_confidence)
    evidence_mass = previous_evidence_mass + effective_confidence
    confidence = 1.0 - (1.0 / (1.0 + evidence_mass))
    updated = {
        "alpha": round(alpha, 6),
        "beta": round(beta, 6),
        "mastery": round(mastery, 6),
        "raw_mastery": round(alpha / (alpha + beta), 6),
        "confidence": round(confidence, 6),
        "attempts": attempts,
        "evidence_mass": round(evidence_mass, 6),
        "last_score": round(observation, 6),
        "last_expected_score": round(expected, 6),
        "last_residual": round(residual, 6),
        "last_difficulty": round(difficulty, 6),
        "last_evidence_kind": evidence_kind,
        "last_evidence_weight": round(evidence_weight, 6),
    }
    profile[knowledge_point] = updated
    state["ability_profile"] = profile
    return updated


def infer_followup_type(score: Mapping[str, Any]) -> str:
    """Classify the follow-up from scores so the LLM cannot steer control flow."""
    if not score.get("followup_needed", False):
        return "none"
    correctness = float(score.get("correctness", 0))
    depth = float(score.get("depth", 0))
    if correctness < 5:
        return "clarify"
    if correctness >= 8 and depth >= 7:
        return "challenge"
    return "deepen"


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left.strip().lower(), right.strip().lower()).ratio()


def _question_priority(
    question: Mapping[str, Any],
    profile: Mapping[str, Any],
    asked: List[Mapping[str, Any]],
) -> Tuple[float, Dict[str, float]]:
    knowledge_point = str(question.get("knowledge_point") or question.get("topic", "general"))
    ability = profile.get(knowledge_point) or {}
    mastery = float(ability.get("mastery", 0.5))
    confidence = float(ability.get("confidence", 0.0))
    attempts = int(ability.get("attempts", 0))
    job_weight = float(question.get("job_weight", 0.5))
    discrimination = float(question.get("discrimination", 0.5))
    difficulty_score = float(question.get("difficulty_score", 0.55))

    knowledge_gap = 1.0 - mastery
    uncertainty = 1.0 - confidence
    coverage_gap = 1.0 / (1.0 + attempts)
    desired_difficulty = max(0.25, min(0.85, mastery + 0.10))
    difficulty_mismatch = abs(difficulty_score - desired_difficulty)
    prompt = str(question.get("prompt", ""))
    repetition = max(
        (_similarity(prompt, str(item.get("prompt", ""))) for item in asked),
        default=0.0,
    )

    components = {
        "job_weight": job_weight,
        "knowledge_gap": knowledge_gap,
        "uncertainty": uncertainty,
        "coverage_gap": coverage_gap,
        "discrimination": discrimination,
        "difficulty_mismatch": difficulty_mismatch,
        "repetition": repetition,
    }
    priority = (
        job_weight * 0.25
        + knowledge_gap * 0.20
        + uncertainty * 0.15
        + coverage_gap * 0.15
        + discrimination * 0.15
        - difficulty_mismatch * 0.05
        - repetition * 0.05
    )
    return priority, components


def select_next_question(state: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    candidates = [
        normalize_question(question, i)
        for i, question in enumerate(state.get("question_candidates") or [])
    ]
    asked = list(state.get("question_plan") or [])
    asked_ids = {str(question.get("id")) for question in asked}
    remaining = [question for question in candidates if str(question.get("id")) not in asked_ids]
    if not remaining:
        return {}, {"reason": "candidate_pool_exhausted"}

    anchor_budget = int(state.get("anchor_count", 0))
    completed = int(state.get("completed_primary_questions", 0))
    anchor_phase = completed < anchor_budget
    remaining_anchors = [question for question in remaining if question.get("is_anchor")]
    if anchor_phase and remaining_anchors:
        remaining = remaining_anchors

    profile = state.get("ability_profile") or {}
    ranked = []
    for question in remaining:
        priority, components = _question_priority(question, profile, asked)
        ranked.append((priority, str(question.get("id")), question, components))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    priority, _, selected, components = ranked[0]

    reasons = []
    if selected.get("is_anchor") and anchor_phase:
        reasons.append("calibration_anchor")
    if components["job_weight"] >= 0.7:
        reasons.append("high_job_weight")
    if components["knowledge_gap"] >= 0.55:
        reasons.append("knowledge_gap")
    if components["uncertainty"] >= 0.6:
        reasons.append("low_confidence")
    if components["coverage_gap"] >= 0.8:
        reasons.append("not_yet_covered")
    if not reasons:
        reasons.append("best_remaining_match")

    decision = {
        "selected_question_id": selected.get("id"),
        "knowledge_point": selected.get("knowledge_point"),
        "is_anchor": bool(selected.get("is_anchor", False)),
        "priority": round(priority, 6),
        "components": {key: round(value, 6) for key, value in components.items()},
        "reasons": reasons,
        "remaining_candidates": len(remaining),
    }
    return selected, decision


def build_calibrated_scoring_summary(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Build a deterministic, difficulty-aware final scoring summary."""
    records = list(state.get("score_records") or [])
    primary_records = [record for record in records if not record.get("is_followup", False)]
    anchor_records = [record for record in primary_records if record.get("is_anchor", False)]

    def average_observation(items: List[Mapping[str, Any]], default: float = 0.5) -> float:
        values = [score_answer(item.get("score") or {}) for item in items]
        return sum(values) / len(values) if values else default

    raw_primary = average_observation(primary_records)
    anchor_score = average_observation(anchor_records, raw_primary)

    profile = state.get("ability_profile") or {}
    assessed = list(profile.values())
    calibrated_ability = (
        sum(float(item.get("mastery", 0.5)) for item in assessed) / len(assessed)
        if assessed
        else raw_primary
    )
    scoring_confidence = (
        sum(float(item.get("confidence", 0.0)) for item in assessed) / len(assessed)
        if assessed
        else 0.0
    )

    candidates = [normalize_question(item, i) for i, item in enumerate(
        state.get("question_candidates") or state.get("question_plan") or []
    )]
    topic_weights: Dict[str, float] = {}
    for item in candidates:
        topic = str(item.get("knowledge_point") or item.get("topic", "general"))
        topic_weights[topic] = max(topic_weights.get(topic, 0.0), float(item.get("job_weight", 0.5)))
    if topic_weights:
        total_weight = sum(topic_weights.values()) or 1.0
        job_match = sum(
            float((profile.get(topic) or {}).get("mastery", 0.5)) * weight
            for topic, weight in topic_weights.items()
        ) / total_weight
        coverage = sum(1 for topic in topic_weights if topic in profile) / len(topic_weights)
    else:
        job_match = calibrated_ability
        coverage = 1.0 if assessed else 0.0

    challenge_records = [
        record for record in records
        if record.get("asked_followup_type") == "challenge"
    ]
    challenge_performance = average_observation(challenge_records, 0.5)
    challenge_bonus = max(0.0, (challenge_performance - 0.5) * 10.0)

    base_score = (
        anchor_score * 100.0 * 0.40
        + calibrated_ability * 100.0 * 0.35
        + job_match * 100.0 * 0.25
    )
    final_score = max(0, min(100, round(base_score + challenge_bonus)))
    return {
        "overall_score": final_score,
        "raw_primary_score": round(raw_primary * 100.0, 2),
        "anchor_score": round(anchor_score * 100.0, 2),
        "calibrated_ability_score": round(calibrated_ability * 100.0, 2),
        "job_match_score": round(job_match * 100.0, 2),
        "challenge_bonus": round(challenge_bonus, 2),
        "scoring_confidence": round(scoring_confidence * 100.0, 2),
        "knowledge_coverage": round(coverage * 100.0, 2),
        "anchor_questions_answered": len(anchor_records),
        "primary_questions_answered": len(primary_records),
        "challenge_questions_answered": len(challenge_records),
    }


def recommendation_for_score(score: int) -> str:
    if score >= 85:
        return "strong_hire"
    if score >= 70:
        return "hire"
    if score >= 60:
        return "weak_hire"
    return "no_hire"


def append_next_question(state: MutableMapping[str, Any]) -> bool:
    question, decision = select_next_question(state)
    if not question:
        return False
    plan = list(state.get("question_plan") or [])
    plan.append(question)
    state["question_plan"] = plan
    state["current_question_idx"] = len(plan) - 1
    decisions = list(state.get("policy_decisions") or [])
    decisions.append(decision)
    state["policy_decisions"] = decisions
    return True
