"""
DAG 节点函数实现：每个节点对应一个 Agent.run() 调用
"""
from __future__ import annotations

from typing import Any, Dict

from loguru import logger

from config import settings
from agents import (
    ChatAgent,
    EvaluatorAgent,
    IntentRouterAgent,
    InterviewerAgent,
    JDAnalyzerAgent,
    QuestionPlannerAgent,
    ResumeAnalyzerAgent,
    StudyPlannerAgent,
)
from orchestration.state import InterviewState

# 单例（避免重复初始化 LLM）
_intent_router = IntentRouterAgent()
_jd_analyzer = JDAnalyzerAgent()
_resume_analyzer = ResumeAnalyzerAgent()
_question_planner = QuestionPlannerAgent()
_interviewer = InterviewerAgent()
_evaluator = EvaluatorAgent()
_study_planner = StudyPlannerAgent()
_chat = ChatAgent()


def node_route(state: InterviewState) -> InterviewState:
    """
    意图路由节点。
    如果调用方已经设置了 intent（CLI/API 直接注入），则跳过 LLM 识别。
    """
    logger.debug(">> node_route")
    if state.get("intent") in (
        "start_interview", "answer_question",
        "input_jd", "upload_resume", "use_skill",
    ):
        logger.debug(f"  intent 已预设为 {state['intent']}，跳过 LLM 路由")
        return dict(state)  # type: ignore[return-value]
    return _intent_router.run(dict(state))  # type: ignore[arg-type, return-value]


def node_jd_analyze(state: InterviewState) -> InterviewState:
    logger.debug(">> node_jd_analyze")
    result = _jd_analyzer.run(dict(state))
    from orchestration.collaboration import set_task_status, submit_artifact

    parsed = result.get("jd_parsed") or {}
    if parsed:
        submit_artifact(
            result,
            task_type="jd_analysis",
            agent_name="jd_analyzer",
            artifact_type="job_profile",
            content={
                "title": parsed.get("title"),
                "level": parsed.get("level"),
                "tech_stack": list(parsed.get("tech_stack") or []),
                "core_competencies": list(parsed.get("core_competencies") or []),
            },
        )
    else:
        set_task_status(
            result,
            task_type="jd_analysis",
            assignee="jd_analyzer",
            status="failed",
            reason="empty_job_profile",
        )
    return result  # type: ignore[return-value]


def node_resume_analyze(state: InterviewState) -> InterviewState:
    logger.debug(">> node_resume_analyze")
    result = _resume_analyzer.run(dict(state))
    from orchestration.collaboration import set_task_status, submit_artifact

    parsed = result.get("resume_parsed") or {}
    if parsed:
        submit_artifact(
            result,
            task_type="resume_analysis",
            agent_name="resume_analyzer",
            artifact_type="candidate_profile",
            content={
                "match_level": parsed.get("match_level"),
                "strengths": list(parsed.get("strengths") or []),
                "weaknesses": list(parsed.get("weaknesses") or []),
                "highlights": list(parsed.get("highlights") or []),
            },
            confidence=min(
                1.0, max(0.0, float(parsed.get("overall_score", 0)) / 100.0)
            ),
        )
    else:
        set_task_status(
            result,
            task_type="resume_analysis",
            assignee="resume_analyzer",
            status="failed",
            reason="empty_candidate_profile",
        )
    return result  # type: ignore[return-value]


def node_rag_retrieve(state: InterviewState) -> InterviewState:
    """调用 RAG 混合检索器，注入 rag_context"""
    logger.debug(">> node_rag_retrieve")
    jd = state.get("jd_parsed", {})
    query_parts = [state.get("interview_topic", "")]
    query_parts.extend(jd.get("tech_stack", []))
    query_parts.extend(jd.get("core_competencies", []))
    query = " ".join(str(part).strip() for part in query_parts if part)
    if not query:
        state["rag_context"] = ""
        state["rag_sources"] = []
        from orchestration.collaboration import set_task_status

        set_task_status(
            state,
            task_type="rag_retrieval",
            assignee="rag_retriever",
            status="skipped",
            reason="empty_retrieval_query",
        )
        return state

    try:
        from rag.query_engine import query_engine
        from rag.metadata_filter import RetrievalMetadataFilter

        metadata_filter = (
            RetrievalMetadataFilter.from_interview_state(state)
            if settings.rag_metadata_filter_enabled
            else None
        )
        results = query_engine.full_query(
            query, top_k=10, metadata_filter=metadata_filter
        )
        state["rag_diagnostics"] = dict(query_engine.last_diagnostics)
        if metadata_filter is not None:
            state["rag_diagnostics"]["metadata_filter_request"] = (
                metadata_filter.to_dict()
            )
    except Exception as e:
        logger.warning("RAG 检索失败，降级为空上下文继续面试: {}", e)
        state["rag_context"] = ""
        state["rag_sources"] = []
        state["rag_diagnostics"] = {"status": "failed", "error_type": type(e).__name__}
        state["rag_error"] = str(e)
        from orchestration.collaboration import set_task_status

        set_task_status(
            state,
            task_type="rag_retrieval",
            assignee="rag_retriever",
            status="failed",
            reason=type(e).__name__,
        )
        return state

    state["rag_context"] = "\n---\n".join(r.get("text", "") for r in results)
    state["rag_sources"] = results
    from orchestration.collaboration import submit_artifact

    submit_artifact(
        state,
        task_type="rag_retrieval",
        agent_name="rag_retriever",
        artifact_type="retrieval_evidence",
        content={
            "source_count": len(results),
            "diagnostics": dict(state.get("rag_diagnostics") or {}),
        },
        confidence=1.0 if results else 0.0,
        evidence=[str(item.get("id")) for item in results if item.get("id")],
    )
    return state


def node_plan_questions(state: InterviewState) -> InterviewState:
    logger.debug(">> node_plan_questions")
    from orchestration.collaboration import (
        interview_coordinator,
        question_reviewer,
        submit_artifact,
    )
    from agents.question_planner import apply_deterministic_recovery
    from orchestration.planning_loop import (
        execute_evidence_requests,
        record_planning_attempt,
        review_feedback,
    )

    working = dict(state)
    interview_coordinator.prepare_question_planning(working)
    working.setdefault("planning_history", [])
    working.setdefault("planning_actions", [])
    max_attempts = max(1, int(settings.agent_planning_max_attempts))
    result = working

    for attempt in range(1, max_attempts + 1):
        result["planning_attempt"] = attempt
        result = _question_planner.run(result)
        requests = list(result.get("planning_evidence_requests") or [])
        action_successes = 0
        # A successful evidence action must be followed by another planning pass
        # so generated questions can actually consume the new evidence.
        if requests and attempt < max_attempts:
            action_successes = execute_evidence_requests(result, requests)
            if action_successes:
                record_planning_attempt(
                    result,
                    review=None,
                    evidence_request_count=len(requests),
                    evidence_action_successes=action_successes,
                )
                result["planning_feedback"] = [
                    "supplementary_evidence_loaded_rebuild_question_plan"
                ]
                continue

        submit_artifact(
            result,
            task_type="question_planning",
            agent_name="question_planner",
            artifact_type="question_candidates",
            content={
                "attempt": attempt,
                "candidate_count": len(result.get("question_candidates") or []),
                "anchor_count": int(result.get("anchor_count", 0)),
                "evidence_request_count": len(requests),
            },
            confidence=1.0 if result.get("question_candidates") else 0.0,
            evidence=[str(item.get("id")) for item in result.get("rag_sources") or []],
        )
        result = question_reviewer.run(result)
        review = dict(result.get("question_review") or {})
        record_planning_attempt(
            result,
            review=review,
            evidence_request_count=len(requests),
            evidence_action_successes=action_successes,
        )
        if not review.get("requires_replan"):
            result["planning_decision"] = str(review.get("decision") or "accepted")
            break
        if attempt < max_attempts:
            result["planning_feedback"] = review_feedback(review)
            logger.info(
                "题目审核要求重规划：attempt={}/{} reasons={}",
                attempt,
                max_attempts,
                result["planning_feedback"],
            )
            continue

        logger.warning("题目重规划预算耗尽，切换确定性恢复题单")
        result = apply_deterministic_recovery(result)
        submit_artifact(
            result,
            task_type="question_planning",
            agent_name="question_planner",
            artifact_type="question_candidates",
            content={
                "attempt": attempt,
                "candidate_count": len(result.get("question_candidates") or []),
                "anchor_count": int(result.get("anchor_count", 0)),
                "recovery": "deterministic",
            },
            confidence=0.6,
            evidence=[str(item.get("id")) for item in result.get("rag_sources") or []],
        )
        result = question_reviewer.run(result)
        result["planning_decision"] = "deterministic_recovery"
        record_planning_attempt(
            result,
            review=result.get("question_review") or {},
            evidence_request_count=0,
            evidence_action_successes=0,
        )
        break

    interview_coordinator.finalize(result)
    return result  # type: ignore[return-value]


def node_ask(state: InterviewState) -> InterviewState:
    logger.debug(">> node_ask")
    return _interviewer.run(dict(state))  # type: ignore[arg-type, return-value]


def node_evaluate_one(state: InterviewState) -> InterviewState:
    """评估单题回答，根据追问判断决定是否推进题目索引"""
    logger.debug(">> node_evaluate_one")
    from orchestration.difficulty_fsm import update_difficulty
    from orchestration.adaptive_policy import append_next_question

    new_state = _evaluator.run(dict(state))
    # Persist every evaluated answer as an idempotent long-term evidence event.
    # The database path is fail-open: interview control flow keeps using the
    # in-memory profile when MySQL is temporarily unavailable.
    try:
        from memory.ability_repository import ability_repository

        record_index = len(new_state.get("score_records") or []) - 1
        persisted = ability_repository.record_interview_evidence(
            new_state, record_index
        )
        if persisted and record_index >= 0:
            record = new_state["score_records"][record_index]
            knowledge_point = str(record.get("knowledge_point") or "general")
            new_state.setdefault("ability_profile", {})[knowledge_point] = persisted
            record["ability_evidence"] = persisted
            new_state.setdefault("ability_memory_events", []).append({
                "record_index": record_index,
                "knowledge_point": knowledge_point,
                "source": "interview",
                "practice_share": persisted.get("practice_share", 0.0),
            })
    except Exception as exc:
        logger.warning("正式面试能力证据写入失败，继续使用会话内画像：{}", exc)
    update_difficulty(new_state)

    # 如果需要追问，不推进索引
    if new_state.get("should_followup", False):
        logger.info(
            "知识点 {} 需要 {} 追问",
            new_state.get("current_question", {}).get("knowledge_point", "general"),
            new_state.get("current_followup_type", "deepen"),
        )
        new_state["interview_finished"] = False
        return new_state  # type: ignore[return-value]

    # 检查是否强制结束
    if new_state.get("force_finish", False):
        new_state["interview_finished"] = True
        logger.info("面试官主动判断结束面试")
        return new_state  # type: ignore[return-value]

    # One primary question is complete only after its follow-up chain ends.
    if "completed_primary_questions" in new_state:
        completed = int(new_state.get("completed_primary_questions", 0)) + 1
    else:
        # Sessions created before the adaptive candidate pool was introduced
        # already contain the full ordered plan.
        completed = int(new_state.get("current_question_idx", 0)) + 1
    new_state["completed_primary_questions"] = completed
    budget = int(new_state.get("total_questions", 5))

    has_candidate_pool = bool(new_state.get("question_candidates"))
    selected_next = False
    if completed < budget:
        if has_candidate_pool:
            selected_next = append_next_question(new_state)
        else:
            # Backward compatibility for persisted sessions with a sequential
            # plan and no candidate-pool state.
            next_index = int(new_state.get("current_question_idx", 0)) + 1
            if next_index < len(new_state.get("question_plan") or []):
                new_state["current_question_idx"] = next_index
                selected_next = True

    if completed >= budget or not selected_next:
        new_state["interview_finished"] = True
        logger.info("面试结束，已完成 {}/{} 道主问题", completed, budget)
    else:
        new_state["interview_finished"] = False
        logger.info(
            "策略引擎选择第 {}/{} 道题：{}",
            completed + 1,
            budget,
            new_state.get("current_question_idx", 0) + 1,
        )

    return new_state  # type: ignore[return-value]


def node_generate_report(state: InterviewState) -> InterviewState:
    logger.debug(">> node_generate_report")
    # 触发报告生成（interview_finished=True 时 evaluator 生成报告）
    return _evaluator.run(dict(state))  # type: ignore[arg-type, return-value]


def node_await_report(state: InterviewState) -> InterviewState:
    """End the interactive graph before Celery generates the final report."""
    state["report_status"] = "pending"
    return state


def node_study_plan(state: InterviewState) -> InterviewState:
    logger.debug(">> node_study_plan")
    return _study_planner.run(dict(state))  # type: ignore[arg-type, return-value]


def node_chat(state: InterviewState) -> InterviewState:
    logger.debug(">> node_chat")
    return _chat.run(dict(state))  # type: ignore[arg-type, return-value]


def node_save_memory(state: InterviewState) -> InterviewState:
    """将本轮薄弱点写入长期记忆，并增量加入专项复习队列。

    复习队列是旁路能力：任何数据库或题目转换错误都只记录日志，不能阻断
    正式面试报告与原有长期记忆流程。
    """
    logger.debug(">> node_save_memory")
    from memory.memory_manager import memory_manager

    user_id = state.get("user_id", "default")
    weaknesses = state.get("weaknesses_to_remember", [])
    if weaknesses:
        memory_manager.append_weaknesses(user_id, weaknesses)
    try:
        from memory.practice_repository import practice_repository

        queued = practice_repository.enqueue_interview_weaknesses(dict(state))
        if queued:
            logger.info("正式面试薄弱题已加入专项复习队列：{} 道", queued)
    except Exception as exc:
        logger.warning("专项复习队列写入失败，已降级且不影响正式面试：{}", exc)
    return state


def node_load_memory(state: InterviewState) -> InterviewState:
    """加载用户长期记忆到状态"""
    logger.debug(">> node_load_memory")
    from memory.memory_manager import memory_manager

    user_id = state.get("user_id", "default")
    state["long_term_weaknesses"] = memory_manager.get_weaknesses(user_id)
    try:
        from memory.ability_repository import ability_repository

        historical = ability_repository.load_profile(
            user_id, str(state.get("target_id") or "")
        )
        # A restored in-session value is newer than the cross-session prior.
        historical.update(dict(state.get("ability_profile") or {}))
        state["ability_profile"] = historical
        state["ability_memory_loaded"] = True
    except Exception as exc:
        state.setdefault("ability_profile", {})
        state["ability_memory_loaded"] = False
        logger.warning("历史能力画像加载失败，按中性先验继续面试：{}", exc)
    return state


def node_skill_dispatch(state: InterviewState) -> InterviewState:
    """分发到 Skill 技能系统"""
    logger.debug(">> node_skill_dispatch")
    from skills.skill_registry import skill_registry

    skill_name = state.get("skill_name", "quiz")
    skill = skill_registry.get(skill_name)
    if skill is None:
        state["agent_reply"] = f"未找到技能：{skill_name}"
        return state
    return skill.run(dict(state))  # type: ignore[arg-type, return-value]
