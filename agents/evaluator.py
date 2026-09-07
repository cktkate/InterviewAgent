"""
实时评估打分 Agent：
- 对候选人的单题回答即时打分
- 维度：正确性 / 深度 / 表达 / 举例
- 判断是否需要追问
- 面试结束后生成多维度评估报告
"""
from __future__ import annotations

from typing import Any, Dict, List

from agents.base_agent import BaseAgent
from agents.output_schemas import FinalReportResult, SingleScoreResult
from orchestration.adaptive_policy import (
    build_calibrated_scoring_summary,
    infer_followup_type,
    recommendation_for_score,
    update_ability_profile,
)
from orchestration.security_policy import (
    UNTRUSTED_DATA_INSTRUCTION,
    record_input_risk,
    wrap_untrusted_data,
)

SINGLE_SCORE_PROMPT = """\
你是严谨的技术面试官。请对候选人的回答进行评分。

题目：__QUESTION__
标准答案要点（如有）：__REFERENCE__
候选人回答：__ANSWER__
是否为追问（followup）：__IS_FOLLOWUP__

评分维度（每项 0-10）：
- correctness：事实正确性
- depth：技术深度
- structure：表达结构
- example：是否举出实际例子

综合判断：
- is_correct：综合是否达标（true/false，correctness>=6 且 depth>=5 视为 true）
- key_missing：遗漏的关键点列表（最多3条，没有则空列表）
- followup_needed：是否建议追问（true/false）
  - 当 key_missing 非空 且 correctness < 8 时建议追问
  - 当候选人回答"不知道"/"不清楚"/"跳过"等时设为 false（无追问意义）
- followup_reason：如果需要追问，说明追问方向（一句话）
- followup_type：none/clarify/deepen/challenge，分别表示不追问、澄清、深挖、挑战
- evaluation_confidence：本次评分置信度（0-1）

输出严格 JSON，不要多余文字。
"""

REPORT_PROMPT = """\
你是资深技术面试官。根据一场完整面试的全部问答记录，生成一份详细的多维度评估报告。

岗位：__JD_TITLE__（__JD_LEVEL__）
单题评分记录：
__SCORES__

系统难度校准评分（最终总分必须以此为准）：
__CALIBRATED_SCORING__

完整问答记录：
__QA_HISTORY__

请输出以下 JSON 格式的评估报告：
{
  "overall_score": 0到100的整数,
  "recommendation": "strong_hire或hire或weak_hire或no_hire",
  "dimension_scores": {
    "technical_knowledge": 0到10的数字（技术知识掌握度）,
    "problem_solving": 0到10的数字（问题解决能力）,
    "system_design": 0到10的数字（系统设计能力）,
    "communication": 0到10的数字（表达与沟通能力）,
    "practical_experience": 0到10的数字（实战经验丰富度）,
    "learning_ability": 0到10的数字（学习潜力与适应力）
  },
  "strengths": ["优势1", "优势2", "优势3"],
  "weaknesses": ["薄弱点1", "薄弱点2", "薄弱点3"],
  "highlights": ["亮点表现1", "亮点表现2"],
  "concerns": ["担忧点1", "担忧点2"],
  "topic_performance": [
    {"topic": "技术主题", "performance": "good或average或weak", "comment": "一句话评价"}
  ],
  "summary": "200字以内的总评",
  "interviewer_comment": "面试官寄语（鼓励+建议，100字以内）"
}
"""


class EvaluatorAgent(BaseAgent):
    name = "evaluator"
    description = "实时评估打分 + 生成多维度报告"

    def score_one(
        self,
        question: str,
        answer: str,
        reference: str = "",
        is_followup: bool = False,
    ) -> Dict[str, Any]:
        question = wrap_untrusted_data(question, source="question")
        answer = wrap_untrusted_data(answer, source="candidate_answer")
        reference = wrap_untrusted_data(reference, source="reference")
        prompt = (
            UNTRUSTED_DATA_INSTRUCTION
            + SINGLE_SCORE_PROMPT
            .replace("__QUESTION__", question)
            .replace("__ANSWER__", answer)
            .replace("__REFERENCE__", reference or "（无）")
            .replace("__IS_FOLLOWUP__", "是" if is_followup else "否")
        )
        return self.invoke_llm_json(
            prompt, "", schema=SingleScoreResult
        )

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:

        # ── 单题评估 ──────────────────────────────────────────────────────────
        if state.get("awaiting_evaluation"):
            q = state.get("current_question_text", "")
            a = state.get("last_user_answer", "")
            record_input_risk(state, source="candidate_answer", text=str(a))
            is_followup = state.get("is_followup", False)
            asked_followup_type = (
                str(state.get("current_followup_type", "deepen"))
                if is_followup
                else "primary"
            )

            # 候选人明确表示不会/跳过，不追问
            skip_keywords = ("不知道", "不清楚", "不会", "跳过", "pass", "不懂",
                             "没接触过", "可以再说一遍", "再说一遍")
            answer_lower = a.lower().strip()
            is_skip = any(kw in answer_lower for kw in skip_keywords) or len(a.strip()) < 10

            current_question = state.get("current_question", {})
            expected_points = current_question.get("expected_points", [])
            reference = "；".join(str(point) for point in expected_points)
            score = self.score_one(
                q,
                a,
                reference=reference,
                is_followup=is_followup,
            )

            # 如果候选人明确跳过，强制不追问
            if is_skip:
                score["followup_needed"] = False
                score["key_missing"] = []
                score["followup_type"] = "none"

            score["followup_type"] = infer_followup_type(score)
            state["current_followup_type"] = score["followup_type"]
            ability_evidence = update_ability_profile(
                state,
                score,
                evidence_kind=asked_followup_type,
            )

            score_record = {
                "question": q,
                "answer": a,
                "score": score,
                "question_id": current_question.get("id"),
                "knowledge_point": current_question.get("knowledge_point"),
                "difficulty_score": current_question.get("difficulty_score", 0.55),
                "is_anchor": bool(current_question.get("is_anchor", False)),
                "is_followup": bool(is_followup),
                "asked_followup_type": asked_followup_type,
                "ability_evidence": ability_evidence,
            }

            state.setdefault("score_records", []).append(score_record)
            state["qa_history"] = state.get("qa_history", []) + [
                {
                    "question": q,
                    "answer": a,
                    "score": score,
                    "is_followup": bool(is_followup),
                    "asked_followup_type": asked_followup_type,
                }
            ]
            state["awaiting_evaluation"] = False
            state["last_correctness"] = score.get("is_correct", False)

            # ── 追问判断 ──────────────────────────────────────────────────────
            followup_needed = score.get("followup_needed", False)
            followup_count = state.get("followup_count", 0)
            max_followup = state.get("max_followup", 2)

            if followup_needed and not is_followup and followup_count < max_followup:
                # 本题需要追问，不推进索引
                state["should_followup"] = True
            elif followup_needed and is_followup and followup_count < max_followup:
                # 已是追问，还可以继续追问
                state["should_followup"] = True
            else:
                # 不追问，推进到下一题
                state["should_followup"] = False

            return state

        # ── 生成整场报告 ──────────────────────────────────────────────────────
        if state.get("interview_finished"):
            jd = state.get("jd_parsed", {})
            calibrated_scoring = build_calibrated_scoring_summary(state)
            state["calibrated_scoring"] = calibrated_scoring

            scores_text = "\n".join(
                f"[Q{i+1}] 题目: {r['question']}\n"
                f"回答摘要: {str(r['answer'])[:200]}\n"
                f"评分详情: {r['score']}\n"
                for i, r in enumerate(state.get("score_records", []))
            )

            qa_text = "\n".join(
                f"[Q{i+1}] 问: {h['question']}\n"
                f"     答: {str(h['answer'])[:300]}\n"
                for i, h in enumerate(state.get("qa_history", []))
            )

            prompt = (
                UNTRUSTED_DATA_INSTRUCTION
                + REPORT_PROMPT
                .replace("__JD_TITLE__", str(jd.get("title", "技术岗")))
                .replace("__JD_LEVEL__", str(jd.get("level", "mid")))
                .replace(
                    "__SCORES__",
                    wrap_untrusted_data(scores_text, source="score_records"),
                )
                .replace("__CALIBRATED_SCORING__", str(calibrated_scoring))
                .replace(
                    "__QA_HISTORY__",
                    wrap_untrusted_data(qa_text, source="qa_history"),
                )
            )

            report = self.invoke_llm_json(
                prompt, "", schema=FinalReportResult
            )
            report["overall_score"] = calibrated_scoring["overall_score"]
            report["recommendation"] = recommendation_for_score(
                calibrated_scoring["overall_score"]
            )
            report["calibrated_scoring"] = calibrated_scoring
            state["final_report"] = report
            state["weaknesses_to_remember"] = report.get("weaknesses", [])

        return state
