"""Tests for bounded JSON repair and strict Pydantic validation."""
from unittest.mock import Mock

import pytest
from loguru import logger

from agents.base_agent import BaseAgent
from agents.output_schemas import QuestionPlanResult, SingleScoreResult


class DummyAgent(BaseAgent):
    name = "dummy"

    def __init__(self, outputs):
        self.logger = logger.bind(agent=self.name)
        self.invoke_llm = Mock(side_effect=outputs)

    def run(self, state):
        return state


VALID_SCORE = """{
  "correctness": 8,
  "depth": 7,
  "structure": 6,
  "example": 5,
  "is_correct": true,
  "key_missing": ["缺少异常处理"],
  "followup_needed": true,
  "followup_reason": "继续考察异常降级"
}"""


def test_retries_after_invalid_json():
    agent = DummyAgent(["not json", VALID_SCORE])

    result = agent.invoke_llm_json(
        "score the answer", "input", schema=SingleScoreResult
    )

    assert result["correctness"] == 8
    assert agent.invoke_llm.call_count == 2
    assert "校验错误" in agent.invoke_llm.call_args.args[1]


def test_retries_after_schema_validation_error():
    invalid_range = VALID_SCORE.replace('"correctness": 8', '"correctness": 18')
    agent = DummyAgent([invalid_range, VALID_SCORE])

    result = agent.invoke_llm_json(
        "score the answer", "input", schema=SingleScoreResult
    )

    assert result["is_correct"] is True
    assert agent.invoke_llm.call_count == 2


def test_raises_after_bounded_retries():
    agent = DummyAgent(["bad-1", "bad-2"])

    with pytest.raises(ValueError, match="after 2 attempts"):
        agent.invoke_llm_json(
            "score the answer",
            "input",
            schema=SingleScoreResult,
            max_output_attempts=2,
        )

    assert agent.invoke_llm.call_count == 2


def test_challenge_followup_does_not_require_a_missing_point():
    result = SingleScoreResult.model_validate(
        {
            "correctness": 9,
            "depth": 8,
            "structure": 8,
            "example": 7,
            "is_correct": True,
            "key_missing": [],
            "followup_needed": True,
            "followup_reason": "增加约束，验证能力边界",
            "followup_type": "challenge",
            "evaluation_confidence": 0.9,
        }
    )

    assert result.followup_type == "challenge"


def test_extra_missing_points_are_truncated_without_llm_retry():
    output = VALID_SCORE.replace(
        '["缺少异常处理"]',
        '["问题定义", "核心机制", "工程权衡", "实际项目案例"]',
    )
    agent = DummyAgent([output])

    result = agent.invoke_llm_json(
        "score the answer", "input", schema=SingleScoreResult
    )

    assert result["key_missing"] == ["问题定义", "核心机制", "工程权衡"]
    assert agent.invoke_llm.call_count == 1


def test_question_plan_coerces_llm_list_strings_without_retry():
    output = """{
      "plan": [{
        "topic": "PyTorch",
        "question_type": "concept",
        "difficulty": "medium",
        "focus": "自动求导",
        "prompt": "请解释张量和自动求导。",
        "knowledge_point": "autograd",
        "difficulty_score": 0.55,
        "expected_points": "张量概念；计算图；反向传播",
        "prerequisites": "Python 编程、基础机器学习",
        "job_weight": 0.8,
        "discrimination": 0.7,
        "estimated_minutes": 4
      }]
    }"""
    agent = DummyAgent([output])

    result = agent.invoke_llm_json(
        "plan questions", "input", schema=QuestionPlanResult
    )

    assert result["plan"][0]["expected_points"] == [
        "张量概念",
        "计算图",
        "反向传播",
    ]
    assert result["plan"][0]["prerequisites"] == ["Python 编程", "基础机器学习"]
    assert agent.invoke_llm.call_count == 1


def test_question_plan_accepts_a_json_array_encoded_as_string():
    item = {
        "topic": "Agent",
        "question_type": "scenario",
        "difficulty": "hard",
        "focus": "工具调用",
        "prompt": "如何保证工具调用可靠性？",
        "expected_points": '["幂等", "重试", "超时"]',
        "prerequisites": [],
    }

    result = QuestionPlanResult.model_validate({"plan": [item]})

    assert result.plan[0].expected_points == ["幂等", "重试", "超时"]
