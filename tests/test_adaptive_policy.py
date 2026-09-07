from orchestration.adaptive_policy import (
    append_next_question,
    build_calibrated_scoring_summary,
    infer_followup_type,
    initialize_candidate_pool,
    recommendation_for_score,
    score_answer,
    update_ability_profile,
)
from agents.question_planner import build_deterministic_anchor_questions


def _question(question_id, topic, weight, difficulty="medium"):
    return {
        "id": question_id,
        "topic": topic,
        "knowledge_point": topic,
        "difficulty": difficulty,
        "prompt": f"question about {topic}",
        "job_weight": weight,
        "discrimination": 0.7,
    }


def test_policy_prefers_high_weight_uncovered_skill():
    state = {"ability_profile": {}}
    initialize_candidate_pool(
        state,
        [_question("Q1", "python", 0.5), _question("Q2", "rag", 0.9)],
    )

    assert append_next_question(state) is True
    assert state["question_plan"][0]["id"] == "Q2"
    assert "high_job_weight" in state["policy_decisions"][0]["reasons"]


def test_ability_profile_uses_all_score_dimensions():
    state = {
        "current_question": _question("Q1", "python", 0.8),
        "ability_profile": {},
    }
    high_score = {
        "correctness": 9,
        "depth": 8,
        "structure": 8,
        "example": 7,
        "evaluation_confidence": 0.9,
    }
    observation = score_answer(high_score)
    updated = update_ability_profile(state, high_score)

    assert observation > 0.8
    assert updated["mastery"] > 0.5
    assert updated["attempts"] == 1


def test_hard_question_seven_is_stronger_evidence_than_easy_question_nine():
    easy_state = {
        "current_question": _question("Q1", "python", 0.8, difficulty="easy"),
        "ability_profile": {},
    }
    hard_state = {
        "current_question": _question("Q2", "python", 0.8, difficulty="hard"),
        "ability_profile": {},
    }
    easy_result = update_ability_profile(
        easy_state,
        {
            "correctness": 9,
            "depth": 9,
            "structure": 9,
            "example": 9,
            "evaluation_confidence": 0.9,
        },
    )
    hard_result = update_ability_profile(
        hard_state,
        {
            "correctness": 7,
            "depth": 7,
            "structure": 7,
            "example": 7,
            "evaluation_confidence": 0.9,
        },
    )

    assert hard_result["last_score"] < easy_result["last_score"]
    assert hard_result["last_residual"] > easy_result["last_residual"]
    assert hard_result["mastery"] > easy_result["mastery"]


def test_failed_challenge_is_downweighted():
    previous = {
        "python": {
            "alpha": 3.0,
            "beta": 1.0,
            "mastery": 0.8,
            "confidence": 0.7,
            "attempts": 2,
            "evidence_mass": 2.0,
        }
    }
    base_state = {
        "current_question": _question("Q1", "python", 0.8, difficulty="hard"),
        "ability_profile": previous,
    }
    weak_score = {
        "correctness": 2,
        "depth": 2,
        "structure": 2,
        "example": 2,
        "evaluation_confidence": 1.0,
    }
    normal = update_ability_profile(
        {**base_state, "ability_profile": {"python": dict(previous["python"])}},
        weak_score,
        evidence_kind="primary",
    )
    challenge = update_ability_profile(
        {**base_state, "ability_profile": {"python": dict(previous["python"])}},
        weak_score,
        evidence_kind="challenge",
    )

    assert challenge["last_evidence_weight"] == 0.25
    assert challenge["mastery"] > normal["mastery"]


def test_followup_type_is_deterministic_when_model_omits_it():
    assert infer_followup_type(
        {"followup_needed": True, "correctness": 3, "depth": 3}
    ) == "clarify"
    assert infer_followup_type(
        {"followup_needed": True, "correctness": 7, "depth": 6}
    ) == "deepen"
    assert infer_followup_type(
        {"followup_needed": True, "correctness": 9, "depth": 8}
    ) == "challenge"


def test_followup_type_cannot_be_overridden_by_model_output():
    assert infer_followup_type(
        {
            "followup_needed": True,
            "followup_type": "challenge",
            "correctness": 2,
            "depth": 2,
        }
    ) == "clarify"


def test_policy_asks_calibration_anchors_before_adaptive_questions():
    anchor_one = _question("Q1", "python", 0.2)
    anchor_one["is_anchor"] = True
    adaptive = _question("Q2", "rag", 1.0)
    anchor_two = _question("Q3", "agent", 0.3)
    anchor_two["is_anchor"] = True
    state = {"ability_profile": {}, "anchor_count": 2}
    initialize_candidate_pool(state, [anchor_one, adaptive, anchor_two])

    assert append_next_question(state) is True
    assert state["question_plan"][0]["is_anchor"] is True
    assert "calibration_anchor" in state["policy_decisions"][0]["reasons"]

    state["completed_primary_questions"] = 1
    assert append_next_question(state) is True
    assert state["question_plan"][1]["is_anchor"] is True


def test_anchor_questions_are_stable_and_do_not_use_resume_data():
    jd = {
        "title": "AI Agent开发工程师",
        "core_competencies": ["Agent状态管理", "RAG"],
        "tech_stack": ["Python"],
    }

    first = build_deterministic_anchor_questions(jd, "", 2)
    second = build_deterministic_anchor_questions(jd, "", 2)

    assert first == second
    assert [item["id"] for item in first] == ["A1", "A2"]
    assert all(item["is_anchor"] for item in first)
    assert all(item["difficulty_score"] == 0.55 for item in first)


def test_interviewer_does_not_paraphrase_anchor_question(monkeypatch):
    from agents.interviewer import InterviewerAgent

    anchor = build_deterministic_anchor_questions(
        {"core_competencies": ["Agent状态管理"]}, "", 1
    )[0]
    agent = InterviewerAgent.__new__(InterviewerAgent)
    monkeypatch.setattr(
        agent,
        "invoke_llm_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("anchor must not call the LLM")
        ),
    )

    result = agent.ask_question(
        {
            "question_plan": [anchor],
            "current_question_idx": 0,
            "total_questions": 1,
            "jd_parsed": {},
            "resume_parsed": {},
            "qa_history": [],
        }
    )

    assert result["current_question_text"] == anchor["prompt"]


def test_final_score_uses_anchor_calibration_and_challenge_only_as_bonus():
    score = {
        "correctness": 8,
        "depth": 8,
        "structure": 8,
        "example": 8,
    }
    weak_challenge = {
        "correctness": 2,
        "depth": 2,
        "structure": 2,
        "example": 2,
    }
    state = {
        "question_candidates": [_question("Q1", "python", 1.0)],
        "ability_profile": {
            "python": {"mastery": 0.75, "confidence": 0.8}
        },
        "score_records": [
            {"score": score, "is_followup": False, "is_anchor": True},
            {
                "score": weak_challenge,
                "is_followup": True,
                "asked_followup_type": "challenge",
            },
        ],
    }

    summary = build_calibrated_scoring_summary(state)

    assert summary["anchor_score"] == 80.0
    assert summary["challenge_bonus"] == 0.0
    assert summary["overall_score"] == 77
    assert recommendation_for_score(summary["overall_score"]) == "hire"


def test_final_report_overrides_llm_score_with_calibrated_score(monkeypatch):
    from agents.evaluator import EvaluatorAgent

    agent = EvaluatorAgent.__new__(EvaluatorAgent)
    llm_report = {
        "overall_score": 10,
        "recommendation": "no_hire",
        "dimension_scores": {
            "technical_knowledge": 7,
            "problem_solving": 7,
            "system_design": 7,
            "communication": 7,
            "practical_experience": 7,
            "learning_ability": 7,
        },
        "strengths": [],
        "weaknesses": [],
        "highlights": [],
        "concerns": [],
        "topic_performance": [],
        "summary": "summary",
        "interviewer_comment": "comment",
    }
    monkeypatch.setattr(agent, "invoke_llm_json", lambda *args, **kwargs: dict(llm_report))
    state = {
        "interview_finished": True,
        "jd_parsed": {},
        "question_candidates": [_question("Q1", "python", 1.0)],
        "ability_profile": {
            "python": {"mastery": 0.75, "confidence": 0.8}
        },
        "score_records": [
            {
                "question": "q",
                "answer": "a",
                "score": {
                    "correctness": 8,
                    "depth": 8,
                    "structure": 8,
                    "example": 8,
                },
                "is_followup": False,
                "is_anchor": True,
            }
        ],
        "qa_history": [],
    }

    result = agent.run(state)

    assert result["final_report"]["overall_score"] == 77
    assert result["final_report"]["recommendation"] == "hire"
    assert result["final_report"]["calibrated_scoring"]["anchor_score"] == 80.0


def test_evaluation_node_selects_another_candidate(monkeypatch):
    from orchestration import nodes

    state = {
        "total_questions": 2,
        "current_difficulty": "medium",
        "consecutive_correct": 0,
        "consecutive_wrong": 0,
    }
    initialize_candidate_pool(
        state,
        [_question("Q1", "python", 0.8), _question("Q2", "rag", 0.7)],
    )
    append_next_question(state)

    class FakeEvaluator:
        @staticmethod
        def run(current_state):
            current_state["should_followup"] = False
            current_state["last_correctness"] = True
            return current_state

    monkeypatch.setattr(nodes, "_evaluator", FakeEvaluator())
    result = nodes.node_evaluate_one(state)

    assert result["completed_primary_questions"] == 1
    assert result["interview_finished"] is False
    assert len(result["question_plan"]) == 2
    assert result["current_question_idx"] == 1


def test_evaluation_node_preserves_legacy_sequential_sessions(monkeypatch):
    from orchestration import nodes

    state = {
        "question_plan": [
            _question("Q1", "python", 0.8),
            _question("Q2", "rag", 0.7),
        ],
        "current_question_idx": 0,
        "total_questions": 2,
        "current_difficulty": "medium",
        "consecutive_correct": 0,
        "consecutive_wrong": 0,
    }

    class FakeEvaluator:
        @staticmethod
        def run(current_state):
            current_state["should_followup"] = False
            current_state["last_correctness"] = True
            return current_state

    monkeypatch.setattr(nodes, "_evaluator", FakeEvaluator())
    result = nodes.node_evaluate_one(state)

    assert result["completed_primary_questions"] == 1
    assert result["interview_finished"] is False
    assert result["current_question_idx"] == 1
