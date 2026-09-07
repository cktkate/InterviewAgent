"""
面试 REST API 路由
POST /interview/start   -> 启动面试，返回第一题
POST /interview/answer  -> 提交回答，返回下一题或报告
POST /interview/skill   -> 触发技能模块
GET  /interview/report/{session_id} -> 获取完整报告
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from api.schemas import (
    AnswerRequest,
    AnswerResponse,
    ReportResponse,
    SkillRequest,
    StartInterviewRequest,
    StartInterviewResponse,
)
from api.security import assert_session_owner, get_current_user_id
from config import settings
from memory.job_target_repository import job_target_repository
from memory.memory_manager import memory_manager

router = APIRouter(prefix="/interview", tags=["面试"])


def _get_graph():
    from orchestration.graph import get_compiled_graph
    return get_compiled_graph()


# ── 启动面试 ──────────────────────────────────────────────────────────────────

@router.post("/start", response_model=StartInterviewResponse)
async def start_interview(
    req: StartInterviewRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    启动一场新面试：
    1. 分析 JD + 简历
    2. RAG 检索题库
    3. 生成题目计划
    4. 返回第一题
    """
    session_id = str(uuid.uuid4())
    graph      = _get_graph()

    target = None
    if req.target_id:
        target = job_target_repository.get_for_user(req.target_id, user_id)
        if target is None or target["status"] == "archived":
            raise HTTPException(status_code=404, detail="求职目标不存在或已归档")

    jd_text = req.jd_text.strip() or (target["jd_text"].strip() if target else "")
    interview_topic = req.interview_topic.strip()
    if not interview_topic and target:
        focus = "、".join(target["focus_areas"])
        interview_topic = target["position_name"] + (f"（重点：{focus}）" if focus else "")
    if not jd_text and not interview_topic:
        raise HTTPException(status_code=422, detail="JD、面试方向或求职目标至少提供一项")
    if not jd_text:
        jd_text = f"请围绕“{interview_topic}”岗位进行技术面试。"

    init_state = {
        "session_id":           session_id,
        "user_id":              user_id,
        "target_id":            req.target_id or "",
        "interview_topic":      interview_topic,
        "jd_text":              jd_text,
        "resume_text":          req.resume_text,
        "intent":               "start_interview",
        "user_input":           "开始面试",
        "current_difficulty":   "medium",
        "current_question_idx": 0,
        "qa_history":           [],
        "score_records":        [],
        "awaiting_answer":      False,
        "awaiting_evaluation":  False,
        "interview_finished":   False,
        "async_report":         settings.celery_enabled,
        "report_status":        "",
        "should_followup":      False,
        "followup_count":       0,
        "max_followup":         2,
        "force_finish":         False,
        "skill_state":          {},
        "total_questions":      req.total_questions,
    }

    try:
        state = graph.invoke(init_state)
    except Exception as e:
        logger.error("面试启动失败: {}", e)
        raise HTTPException(status_code=500, detail=f"面试启动失败: {e}")

    question = state.get("current_question_text", "")
    if not question:
        raise HTTPException(status_code=500, detail="题目生成失败，请检查 JD 内容")

    memory_manager.save_session(session_id, state)
    if req.target_id:
        job_target_repository.link_session(req.target_id, session_id, user_id)

    return StartInterviewResponse(
        session_id=session_id,
        target_id=req.target_id,
        question=question,
        question_index=state.get("current_question_idx", 0),
        total_questions=int(state.get("total_questions", req.total_questions)),
        difficulty=state.get("current_difficulty", "medium"),
        jd_title=state.get("jd_parsed", {}).get("title", ""),
    )


# ── 提交回答 ──────────────────────────────────────────────────────────────────

@router.post("/answer", response_model=AnswerResponse)
async def submit_answer(
    req: AnswerRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    提交当前题目的回答：
    - 评估得分
    - 判断是否追问
    - 返回下一题或面试报告
    """
    state = memory_manager.load_session(req.session_id)
    if not state:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    assert_session_owner(state, user_id)

    graph = _get_graph()

    state["user_input"]          = req.answer
    state["last_user_answer"]    = req.answer
    state["intent"]              = "answer_question"
    state["awaiting_evaluation"] = True
    state["awaiting_answer"]     = False

    try:
        state = graph.invoke(state)
    except Exception as e:
        logger.error("answer 处理失败: {}", e)
        raise HTTPException(status_code=500, detail=f"回答处理失败: {e}")

    memory_manager.save_session(req.session_id, state)

    if state.get("interview_finished") and state.get("async_report"):
        from orchestration.report_dispatch import dispatch_report

        state = dispatch_report(state)

    return AnswerResponse(
        session_id=req.session_id,
        interview_finished=state.get("interview_finished", False),
        should_followup=state.get("should_followup", False),
        question=state.get("current_question_text", ""),
        question_index=state.get("current_question_idx", 0),
        total_questions=int(state.get("total_questions", 0)),
        difficulty=state.get("current_difficulty", "medium"),
        final_report=state.get("final_report"),
        study_plan=state.get("study_plan"),
        github_recommendations=state.get("github_recommendations", []),
    )


# ── 触发技能 ──────────────────────────────────────────────────────────────────

@router.post("/skill")
async def use_skill(
    req: SkillRequest,
    user_id: str = Depends(get_current_user_id),
):
    """触发技能模块（quiz / teach / project / compare）"""
    state = memory_manager.load_session(req.session_id)
    if not state:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    assert_session_owner(state, user_id)

    graph = _get_graph()

    state["intent"]     = "use_skill"
    state["skill_name"] = req.skill_name
    state["user_input"] = req.user_input

    try:
        state = graph.invoke(state)
    except Exception as e:
        logger.error("skill 处理失败: {}", e)
        raise HTTPException(status_code=500, detail=f"技能调用失败: {e}")

    memory_manager.save_session(req.session_id, state)

    return {
        "session_id":  req.session_id,
        "skill_name":  req.skill_name,
        "agent_reply": state.get("agent_reply", ""),
        "skill_state": state.get("skill_state", {}),
    }


# ── 获取报告 ──────────────────────────────────────────────────────────────────

@router.get("/report/{session_id}", response_model=ReportResponse)
async def get_report(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """获取已完成面试的评估报告和复习计划"""
    state = memory_manager.load_session(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    assert_session_owner(state, user_id)

    final_report = state.get("final_report")
    if not final_report:
        raise HTTPException(status_code=202, detail="面试尚未结束或报告生成中")

    return ReportResponse(
        session_id=session_id,
        target_id=state.get("target_id") or None,
        final_report=final_report,
        study_plan=state.get("study_plan", {}),
        github_recommendations=state.get("github_recommendations", []),
    )
