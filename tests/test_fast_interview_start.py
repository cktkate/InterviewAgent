"""Question-planner latency optimization tests."""
from __future__ import annotations


def _draft(index: int) -> dict:
    return {
        "topic": "RAG",
        "question_type": "scenario" if index % 2 else "concept",
        "difficulty": "hard" if index == 4 else "medium",
        "focus": f"focus-{index}",
        "prompt": f"question-{index}",
        "knowledge_point": f"rag-{index}",
        "expected_points": ["point-a", "point-b"],
    }


def test_question_planner_uses_compact_pool_and_derives_policy_fields(monkeypatch):
    from agents.question_planner import QuestionPlannerAgent

    agent = QuestionPlannerAgent()
    captured = {}

    def fake_invoke(system_prompt, user_prompt, **kwargs):
        captured.update(kwargs)
        return {"plan": [_draft(index) for index in range(5)]}

    monkeypatch.setattr(agent, "invoke_llm_json", fake_invoke)
    result = agent.run({
        "total_questions": 5,
        "interview_topic": "RAG",
        "jd_parsed": {
            "title": "AI 应用工程师",
            "level": "mid",
            "tech_stack": ["RAG"],
            "core_competencies": ["检索增强生成"],
        },
    })

    assert len(result["question_candidates"]) == 7
    assert captured["max_output_attempts"] == 2
    generated = [item for item in result["question_candidates"] if not item["is_anchor"]]
    assert all(item["job_weight"] == 0.85 for item in generated)
    assert generated[-1]["difficulty_score"] == 0.80
    assert generated[-1]["estimated_minutes"] == 5
