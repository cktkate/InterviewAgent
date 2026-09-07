"""
WebSocket 路由：/ws/{session_id}

消息协议：
  客户端->服务端:
    {"type":"start", "jd_text":"...", "resume_text":"...",
     "total_questions":5, "voice":"longxiaochun"}
    {"type":"answer",  "text":"用户回答"}
    {"type":"force_finish"}           - 强制结束面试生成报告
    {"type":"get_memory"}             - 主动拉取记忆数据
    {"type":"clear_long_memory"}      - 清除长期记忆薄弱点
    {"type":"get_rag"}                - 查看RAG检索结果
    {"type":"get_report_status"}      - 查询Celery报告任务状态
    {"type":"tts_speak", "text":"...", "voice":"..."} - 朗读指定文本
    {"type":"skill", "skill_name":"...", "user_input":"..."}
    {"type":"route", "text":"自然语言请求", "voice":"..."}
    {"type":"ping"}

  服务端->客户端:
    {"type":"agent_stream_start", "stream_id":"...", "operation":"start_interview",
                                  "progress":3, "message":"正在准备面试..."}
    {"type":"agent_status", "stream_id":"...", "node":"rag_retrieve",
                              "status":"completed", "progress":65,
                              "message":"知识检索完成"}
    {"type":"agent_stream_end", "stream_id":"...", "status":"completed",
                                  "progress":100}
    {"type":"text_stream_start", "stream_id":"...", "kind":"question"}
    {"type":"text_stream_delta", "stream_id":"...", "delta":"..."}
    {"type":"text_stream_end", "stream_id":"...", "text":"..."}
    {"type":"question",  "text":"...", "index":1, "total":5,
                         "difficulty":"medium", "memory_snapshot":{...}}
    {"type":"followup",  "text":"...", "memory_snapshot":{...}}
    {"type":"finished",  "farewell":"...", "report":{},
                         "study_plan":{}, "github_recommendations":[],
                         "memory_snapshot":{...}}
    {"type":"skill_reply", "skill_name":"...", "text":"..."}
    {"type":"report_generating", "task_id":"...", "status":"queued"}
    {"type":"stt_status",  "request_id":"...",
                           "status":"accepted|loading_model|processing|completed|failed"}
    {"type":"stt_result",  "text":"...", "message":""}
    {"type":"memory_data", "short_term":{...}, "long_term":{...}}
    {"type":"rag_data",    "rag_sources":[...], "rag_context":"...", "total":N}
    {"type":"toast",       "msg":"...", "level":"success|warning|error|info"}
    {"type":"tts_start"}  {"type":"tts_end"}
    {"type":"error",       "message":"..."}
    {"type":"pong"}

  服务端->客户端(二进制): TTS 音频数据块
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from audio.stt import stt_engine
from audio.tts import tts_engine
from api.security import TokenError, decode_access_token
from config import settings
from memory.memory_manager import memory_manager

router = APIRouter(tags=["WebSocket"])
_stt_slots = asyncio.Semaphore(max(1, int(settings.stt_max_concurrent_jobs)))

_NODE_LABELS = {
    "route": "意图路由",
    "load_memory": "历史能力加载",
    "jd_analyze": "岗位要求分析",
    "resume_analyze": "简历能力解析",
    "rag_retrieve": "知识检索",
    "plan_questions": "自适应题目规划",
    "ask": "面试问题生成",
    "evaluate_one": "回答评估与策略决策",
    "generate_report": "面试报告生成",
    "await_report": "异步报告调度",
    "study_plan": "学习计划生成",
    "save_memory": "能力画像沉淀",
    "skill_dispatch": "Skill 调度",
    "chat": "对话回复生成",
}

_OPERATION_START_MESSAGES = {
    "start_interview": "正在准备本轮面试...",
    "answer_question": "正在评估回答并规划下一步...",
    "use_skill": "正在处理你的请求...",
    "force_finish": "正在生成面试报告...",
    "chat": "正在组织回复...",
}

# Progress advances only when a real graph node has completed. Values represent
# business-stage completion, not a synthetic timer or an estimate of wall time.
_OPERATION_NODE_PROGRESS = {
    "start_interview": {
        "route": 5,
        "load_memory": 12,
        "jd_analyze": 30,
        "resume_analyze": 42,
        "rag_retrieve": 65,
        "plan_questions": 92,
        "ask": 100,
    },
    "answer_question": {
        "route": 5,
        "evaluate_one": 45,
        "ask": 100,
        "await_report": 100,
        "generate_report": 72,
        "study_plan": 90,
        "save_memory": 100,
    },
    "use_skill": {"route": 10, "skill_dispatch": 100},
    "chat": {"route": 10, "chat": 100},
}


def _get_graph():
    from orchestration.graph import get_compiled_graph
    return get_compiled_graph()


def _get_voice(state: Optional[Dict[str, Any]]) -> Optional[str]:
    """读取会话音色；兼容旧字段 _voice。"""
    if not state:
        return None
    return state.get("tts_voice") or state.get("_voice")


def _resolve_farewell(state: Dict[str, Any]) -> str:
    """Do not mistake the last question cached in agent_reply for a farewell."""
    default = "感谢你参与本次模拟面试！报告已生成，祝你面试顺利！"
    candidate = str(state.get("farewell") or state.get("agent_reply") or "").strip()
    current_question = str(state.get("current_question_text") or "").strip()
    if not candidate or candidate == current_question:
        return default
    return candidate


def _extract_interview_topic(text: str) -> str:
    """Extract a concise topic from a natural-language interview request."""
    topic = text.removeprefix("/jd").strip()
    topic = re.sub(
        r"^(?:请)?(?:帮我)?(?:我想要?|我要)?(?:进行|开始|来一场)?(?:模拟)?面试",
        "",
        topic,
    )
    topic = re.sub(r"(?:岗位|方向)$", "", topic)
    return topic.strip(" ：:，,。") or text.strip()


def _topic_as_minimal_jd(topic: str) -> str:
    return (
        f"面试目标方向：{topic}\n"
        f"岗位核心技术栈：{topic}\n"
        f"请围绕{topic}的核心概念、工程实现、系统设计、问题排查和项目实践开展技术面试。"
    )


# ── 基础发送 ──────────────────────────────────────────────────────────────────

async def _send_json(ws: WebSocket, data: Dict[str, Any]) -> bool:
    """安全发送 JSON"""
    try:
        await ws.send_text(json.dumps(data, ensure_ascii=False))
        return True
    except Exception as e:
        logger.warning("WebSocket 发送失败: {}", e)
        return False


async def _forward_task_events(ws: WebSocket, session_id: str) -> None:
    """Bridge cross-process Redis Stream events to this WebSocket client."""
    from tasks.event_bus import stream_task_events

    async for event in stream_task_events(session_id):
        sent = await _send_json(ws, {
            "type": "report_progress",
            "task_id": event.get("task_id", ""),
            "status": event.get("status", ""),
            "stage": event.get("stage", ""),
            "progress": event.get("progress", 0),
            "attempt": event.get("attempt", 0),
            "message": event.get("message", ""),
            "event_id": event.get("event_id", ""),
        })
        if not sent:
            return


async def _stream_graph(
    ws: WebSocket,
    graph: Any,
    input_state: Dict[str, Any],
    operation: str,
) -> Dict[str, Any]:
    """Run LangGraph as node updates and expose only safe progress metadata."""
    stream_id = uuid.uuid4().hex
    state = dict(input_state)
    current_progress = 3
    await _send_json(ws, {
        "type": "agent_stream_start",
        "stream_id": stream_id,
        "operation": operation,
        "status": "running",
        "progress": 3,
        "message": _OPERATION_START_MESSAGES.get(operation, "正在处理..."),
    })

    try:
        async for event in graph.astream(state, stream_mode="updates"):
            if not isinstance(event, dict):
                continue
            for node_name, update in event.items():
                if node_name == "__interrupt__":
                    continue
                if isinstance(update, dict):
                    state.update(update)
                label = _NODE_LABELS.get(node_name, node_name)
                configured_progress = _OPERATION_NODE_PROGRESS.get(operation, {}).get(node_name)
                if configured_progress is not None:
                    current_progress = max(current_progress, configured_progress)
                await _send_json(ws, {
                    "type": "agent_status",
                    "stream_id": stream_id,
                    "operation": operation,
                    "node": node_name,
                    "status": "completed",
                    "progress": current_progress,
                    "message": f"{label}完成",
                })

        await _send_json(ws, {
            "type": "agent_stream_end",
            "stream_id": stream_id,
            "operation": operation,
            "status": "completed",
            "progress": 100,
            "message": "处理完成，正在整理输出...",
            "run_id": state.get("runtime_run_id", ""),
            "trace_id": state.get("runtime_trace_id", ""),
        })
        return state
    except Exception:
        await _send_json(ws, {
            "type": "agent_stream_end",
            "stream_id": stream_id,
            "operation": operation,
            "status": "failed",
        })
        raise


async def _send_text_stream(
    ws: WebSocket,
    text: str,
    kind: str,
    metadata: Optional[Dict[str, Any]] = None,
    chunk_size: int = 16,
) -> str:
    """Progressively deliver validated text; structured LLM output stays atomic."""
    stream_id = uuid.uuid4().hex
    start_message: Dict[str, Any] = {
        "type": "text_stream_start",
        "stream_id": stream_id,
        "kind": kind,
    }
    start_message.update(metadata or {})
    await _send_json(ws, start_message)

    safe_chunk_size = max(1, chunk_size)
    for offset in range(0, len(text), safe_chunk_size):
        await _send_json(ws, {
            "type": "text_stream_delta",
            "stream_id": stream_id,
            "delta": text[offset:offset + safe_chunk_size],
        })
        await asyncio.sleep(0)

    await _send_json(ws, {
        "type": "text_stream_end",
        "stream_id": stream_id,
        "kind": kind,
        "text": text,
    })
    return stream_id


async def _send_tts(ws: WebSocket, text: str, voice: Optional[str] = None) -> None:
    """
    TTS 合成推流。
    无论是否有音频数据，都发 tts_start / tts_end，
    确保前端 TtsPlayer 指示器正确关闭。
    """
    await _send_json(ws, {"type": "tts_start"})

    if not text or not text.strip():
        await _send_json(ws, {"type": "tts_end"})
        return

    if not tts_engine.available:
        logger.debug("TTS 不可用（DASHSCOPE_API_KEY 未配置），跳过合成")
        await _send_json(ws, {"type": "tts_end"})
        return

    try:
        chunk_count = 0
        byte_count = 0
        async for chunk in tts_engine.synthesize_stream(text, voice=voice):
            await ws.send_bytes(chunk)
            chunk_count += 1
            byte_count += len(chunk)
        logger.info("TTS 推流完成: {} 块, {} bytes, voice={}", chunk_count, byte_count, voice)
    except Exception as e:
        logger.error("TTS 推流异常: {}", e)
    finally:
        await _send_json(ws, {"type": "tts_end"})


async def _handle_stt_audio(ws: WebSocket, audio_bytes: bytes) -> None:
    """在线程池执行本地 STT，避免 CPU 推理阻塞 WebSocket 事件循环。"""
    request_id = uuid.uuid4().hex
    byte_count = len(audio_bytes)
    max_bytes = max(1, int(settings.stt_max_audio_bytes))
    await _send_json(ws, {
        "type": "stt_status",
        "request_id": request_id,
        "status": "accepted",
        "audio_bytes": byte_count,
    })

    if byte_count > max_bytes:
        message = f"录音过大，请缩短后重试（最大 {max_bytes // (1024 * 1024)} MB）"
        await _send_json(ws, {
            "type": "stt_status", "request_id": request_id,
            "status": "failed", "code": "audio_too_large", "message": message,
        })
        await _send_json(ws, {
            "type": "stt_result", "request_id": request_id,
            "text": "", "message": message,
        })
        return

    if not stt_engine.available:
        message = "本地语音识别不可用，请使用文字输入"
        await _send_json(ws, {
            "type": "stt_status", "request_id": request_id,
            "status": "failed", "code": "stt_unavailable", "message": message,
        })
        await _send_json(ws, {
            "type": "stt_result", "request_id": request_id,
            "text": "", "message": message,
        })
        return

    if not stt_engine.is_loaded:
        await _send_json(ws, {
            "type": "stt_status", "request_id": request_id,
            "status": "loading_model", "message": "首次使用，正在加载语音模型",
        })

    started = time.perf_counter()
    try:
        async with _stt_slots:
            await _send_json(ws, {
                "type": "stt_status", "request_id": request_id,
                "status": "processing", "message": "语音识别中",
            })
            text = await asyncio.wait_for(
                asyncio.to_thread(stt_engine.transcribe_bytes, audio_bytes, False),
                timeout=max(0.01, float(settings.stt_transcribe_timeout_seconds)),
            )
    except asyncio.TimeoutError:
        message = "语音识别超时，请缩短录音后重试"
        await _send_json(ws, {
            "type": "stt_status", "request_id": request_id,
            "status": "failed", "code": "transcribe_timeout", "message": message,
        })
        await _send_json(ws, {
            "type": "stt_result", "request_id": request_id,
            "text": "", "message": message,
        })
        return
    except Exception as exc:
        logger.exception("STT 工作线程异常: {}", exc)
        message = "语音识别失败，请重试或使用文字输入"
        await _send_json(ws, {
            "type": "stt_status", "request_id": request_id,
            "status": "failed", "code": "transcribe_error", "message": message,
        })
        await _send_json(ws, {
            "type": "stt_result", "request_id": request_id,
            "text": "", "message": message,
        })
        return

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    if not text and not stt_engine.available:
        message = "语音模型加载失败，请使用文字输入或检查 STT 配置"
        await _send_json(ws, {
            "type": "stt_status", "request_id": request_id,
            "status": "failed", "code": "model_load_failed",
            "message": message, "duration_ms": duration_ms,
        })
        await _send_json(ws, {
            "type": "stt_result", "request_id": request_id,
            "text": "", "message": message,
        })
        return
    await _send_json(ws, {
        "type": "stt_status", "request_id": request_id,
        "status": "completed", "duration_ms": duration_ms,
        "recognized": bool(text),
    })
    await _send_json(ws, {
        "type": "stt_result", "request_id": request_id,
        "text": text,
        "message": "" if text else "未识别到语音，请重试",
    })


# ── State 辅助 ────────────────────────────────────────────────────────────────

def _safe_extract_qa_history(state: Dict) -> List[Dict]:
    """
    安全提取 qa_history，确保每条记录完全可 JSON 序列化。
    qa_history 格式: [{"question": str, "answer": str, "score": dict}]
    """
    raw = state.get("qa_history", [])
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        score = item.get("score", {})
        if not isinstance(score, dict):
            score = {}
        result.append({
            "question": str(item.get("question", "")),
            "answer":   str(item.get("answer", "")),
            "score": {
                "correctness":     int(score.get("correctness", 0)),
                "depth":           int(score.get("depth", 0)),
                "structure":       int(score.get("structure", 0)),
                "example":         int(score.get("example", 0)),
                "is_correct":      bool(score.get("is_correct", False)),
                "followup_needed": bool(score.get("followup_needed", False)),
                "key_missing":     [str(k) for k in score.get("key_missing", [])],
            },
        })
    return result


def _build_short_term_snapshot(state: Dict) -> Dict:
    """构建短期记忆快照，随每条 WS 消息透传给前端"""
    return {
        "qa_history":           _safe_extract_qa_history(state),
        "current_question_idx": int(state.get("current_question_idx", 0)),
        "current_difficulty":   str(state.get("current_difficulty", "medium")),
        "score_records_count":  len(state.get("score_records", [])),
        "total_questions":      int(state.get("total_questions", 0)),
        "long_term_weaknesses": list(state.get("long_term_weaknesses", [])),
    }


async def _get_fresh_long_term(user_id: str, fallback: list) -> list:
    """从 MySQL 获取最新长期记忆，失败时用 fallback"""
    try:
        result = memory_manager.get_weaknesses(user_id)
        return result if result is not None else fallback
    except Exception:
        return fallback


async def _send_memory_data(ws: WebSocket, state: Dict, user_id: str) -> None:
    """主动推送完整记忆数据给前端"""
    fresh_weaknesses = await _get_fresh_long_term(
        user_id, list(state.get("long_term_weaknesses", []))
    )
    snapshot = _build_short_term_snapshot(state)
    snapshot["long_term_weaknesses"] = fresh_weaknesses
    await _send_json(ws, {
        "type":       "memory_data",
        "short_term": snapshot,
        "long_term":  {"weaknesses": fresh_weaknesses},
    })


# ── 业务处理函数 ──────────────────────────────────────────────────────────────

async def _handle_start(
    ws: WebSocket,
    data: Dict[str, Any],
    session_id: str,
    graph,
    authenticated_user_id: str,
) -> Optional[Dict]:
    """处理 start 消息，初始化面试"""
    jd_text         = data.get("jd_text", "").strip()
    target_id       = str(data.get("target_id") or "").strip()
    interview_topic = data.get("interview_topic", "").strip()
    resume_text     = data.get("resume_text", "").strip()
    total_questions = int(data.get("total_questions", 5))
    user_id         = authenticated_user_id
    voice           = data.get("voice")

    target = None
    if target_id:
        from memory.job_target_repository import job_target_repository
        target = job_target_repository.get_for_user(target_id, user_id)
        if target is None or target["status"] == "archived":
            await _send_json(ws, {"type": "error", "message": "求职目标不存在或已归档"})
            return None
        jd_text = jd_text or target["jd_text"].strip()
        if not interview_topic:
            focus = "、".join(target["focus_areas"])
            interview_topic = target["position_name"] + (f"（重点：{focus}）" if focus else "")

    if not jd_text and interview_topic:
        jd_text = f"请围绕“{interview_topic}”岗位进行技术面试。"
    if not jd_text:
        await _send_json(ws, {"type": "error", "message": "JD 内容不能为空"})
        return None

    # 加载历史长期记忆
    long_term_weaknesses = await _get_fresh_long_term(user_id, [])

    init_state = {
        "session_id":           session_id,
        "user_id":              user_id,
        "target_id":            target_id,
        "interview_topic":      interview_topic,
        "jd_text":              jd_text,
        "resume_text":          resume_text,
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
        "total_questions":      total_questions,
        "_voice":               voice,
        "tts_voice":            voice,
        "long_term_weaknesses": long_term_weaknesses,
    }

    try:
        state = await _stream_graph(ws, graph, init_state, "start_interview")
    except Exception as e:
        logger.error("面试启动失败: {}", e)
        await _send_json(ws, {"type": "error", "message": f"面试启动失败: {e}"})
        return None

    # 保持长期记忆
    if not state.get("long_term_weaknesses"):
        state["long_term_weaknesses"] = long_term_weaknesses

    memory_manager.save_session(session_id, state)
    if target_id:
        job_target_repository.link_session(target_id, session_id, user_id)

    question = state.get("current_question_text", "")
    if not question:
        await _send_json(ws, {"type": "error", "message": "题目生成失败"})
        return None

    text_stream_id = await _send_text_stream(ws, question, "question", {
        "index": int(state.get("current_question_idx", 0)) + 1,
        "total": int(state.get("total_questions", 0)),
        "difficulty": state.get("current_difficulty", "medium"),
    })
    await _send_json(ws, {
        "type":            "question",
        "stream_id":       text_stream_id,
        "text":            question,
        "index":           int(state.get("current_question_idx", 0)) + 1,
        "total":           int(state.get("total_questions", 0)),
        "difficulty":      state.get("current_difficulty", "medium"),
        "jd_title":        state.get("jd_parsed", {}).get("title", ""),
        "memory_snapshot": _build_short_term_snapshot(state),
    })
    await _send_tts(ws, question, voice=voice)
    return state


async def _handle_answer(
    ws: WebSocket,
    answer_text: str,
    state: Dict,
    graph,
) -> Dict:
    """处理用户回答"""
    session_id = state.get("session_id", "")
    voice      = _get_voice(state)

    state["user_input"]          = answer_text
    state["last_user_answer"]    = answer_text
    state["intent"]              = "answer_question"
    state["awaiting_evaluation"] = True
    state["awaiting_answer"]     = False

    try:
        state = await _stream_graph(ws, graph, state, "answer_question")
    except Exception as e:
        logger.error("回答处理失败: {}", e)
        await _send_json(ws, {"type": "error", "message": f"处理失败: {e}"})
        return state

    memory_manager.save_session(session_id, state)

    if state.get("interview_finished") and state.get("async_report"):
        from orchestration.report_dispatch import dispatch_report

        state = dispatch_report(state)
    snapshot = _build_short_term_snapshot(state)

    # ── 面试结束 ──────────────────────────────────────────────────────────
    if bool(state.get("interview_finished", False)):
        if state.get("report_status") in ("pending", "queued", "processing", "retrying"):
            await _send_json(ws, {
                "type": "report_generating",
                "task_id": state.get("report_task_id", ""),
                "status": state.get("report_status", "queued"),
                "stage": state.get("report_stage", "queued"),
                "progress": state.get("report_progress", 5),
                "event_stream": settings.task_event_stream_enabled,
                "message": "面试已完成，正在生成评估报告",
            })
            return state

        farewell = _resolve_farewell(state)

        fresh_w = await _get_fresh_long_term(
            state.get("user_id", "web_user"),
            list(state.get("long_term_weaknesses", []))
        )
        snapshot["long_term_weaknesses"] = fresh_w

        text_stream_id = await _send_text_stream(ws, farewell, "finished")
        await _send_json(ws, {
            "type":                   "finished",
            "stream_id":              text_stream_id,
            "farewell":               farewell,
            "report":                 state.get("final_report", {}),
            "study_plan":             state.get("study_plan", {}),
            "github_recommendations": state.get("github_recommendations", []),
            "memory_snapshot":        snapshot,
        })
        await _send_tts(ws, farewell, voice=voice)
        return state

    # ── 追问 ──────────────────────────────────────────────────────────────
    if bool(state.get("should_followup", False)):
        followup_q = state.get("current_question_text", "")
        text_stream_id = await _send_text_stream(ws, followup_q, "followup")
        await _send_json(ws, {
            "type":            "followup",
            "stream_id":       text_stream_id,
            "text":            followup_q,
            "memory_snapshot": snapshot,
        })
        await _send_tts(ws, followup_q, voice=voice)
        return state

    # ── 下一题 ────────────────────────────────────────────────────────────
    next_q = state.get("current_question_text", "")
    text_stream_id = await _send_text_stream(ws, next_q, "question", {
        "index": int(state.get("current_question_idx", 0)) + 1,
        "total": int(state.get("total_questions", 0)),
        "difficulty": state.get("current_difficulty", "medium"),
    })
    await _send_json(ws, {
        "type":            "question",
        "stream_id":       text_stream_id,
        "text":            next_q,
        "index":           int(state.get("current_question_idx", 0)) + 1,
        "total":           int(state.get("total_questions", 0)),
        "difficulty":      state.get("current_difficulty", "medium"),
        "memory_snapshot": snapshot,
    })
    await _send_tts(ws, next_q, voice=voice)
    return state


async def _handle_resume(ws: WebSocket, state: Dict) -> None:
    """Return a persisted session without invoking the graph again."""
    if not state:
        await _send_json(ws, {
            "type": "session_expired",
            "message": "上次面试会话不存在或已过期，请重新开始面试",
        })
        return

    report_status = state.get("report_status", "")
    if state.get("interview_finished"):
        if report_status in ("pending", "queued", "processing", "retrying"):
            await _send_json(ws, {
                "type": "report_generating",
                "task_id": state.get("report_task_id", ""),
                "status": report_status,
                "stage": state.get("report_stage", "queued"),
                "progress": state.get("report_progress", 5),
                "event_stream": settings.task_event_stream_enabled,
                "message": "已恢复会话，正在生成评估报告",
            })
            return

        await _send_json(ws, {
            "type": "finished",
            "farewell": _resolve_farewell(state),
            "report": state.get("final_report", {}),
            "study_plan": state.get("study_plan", {}),
            "github_recommendations": state.get("github_recommendations", []),
            "memory_snapshot": _build_short_term_snapshot(state),
        })
        return

    question = state.get("current_question_text", "")
    if not question:
        await _send_json(ws, {
            "type": "session_expired",
            "message": "上次会话状态不完整，请重新开始面试",
        })
        return

    plan = state.get("question_plan", [])
    await _send_json(ws, {
        "type": "session_resumed",
        "text": question,
        "index": int(state.get("current_question_idx", 0)) + 1,
        "total": int(state.get("total_questions", len(plan))),
        "difficulty": state.get("current_difficulty", "medium"),
        "is_followup": bool(state.get("is_followup", False)),
        "memory_snapshot": _build_short_term_snapshot(state),
    })


async def _handle_force_finish(
    ws: WebSocket,
    state: Dict,
    graph,
) -> Dict:
    """强制结束面试，直接生成报告（跳过剩余题目）"""
    session_id = state.get("session_id", "")
    voice      = _get_voice(state)

    logger.info("强制结束面试: session={}", session_id)

    # 标记结束
    state["interview_finished"] = True
    state["force_finish"]       = True
    state["awaiting_evaluation"] = False

    # 如果当前题未作答，补一条空记录
    cur_q = state.get("current_question_text", "")
    if cur_q and not any(
        r.get("question") == cur_q
        for r in state.get("score_records", [])
    ):
        state.setdefault("score_records", []).append({
            "question": cur_q,
            "answer":   "（候选人主动结束，未作答）",
            "score": {
                "correctness": 0, "depth": 0, "structure": 0,
                "example": 0, "is_correct": False,
                "followup_needed": False, "key_missing": [],
            },
        })
        state["qa_history"] = state.get("qa_history", []) + [{
            "question": cur_q,
            "answer":   "（候选人主动结束，未作答）",
            "score":    {"is_correct": False},
        }]

    if settings.celery_enabled:
        from orchestration.report_dispatch import dispatch_report

        state["async_report"] = True
        state["report_status"] = "pending"
        memory_manager.save_session(session_id, state)
        state = dispatch_report(state)
        if state.get("report_status") in ("queued", "processing", "retrying"):
            await _send_json(ws, {
                "type": "report_generating",
                "task_id": state.get("report_task_id", ""),
                "status": state.get("report_status", "queued"),
                "stage": state.get("report_stage", "queued"),
                "progress": state.get("report_progress", 5),
                "event_stream": settings.task_event_stream_enabled,
                "message": "面试已结束，正在生成评估报告",
            })
            return state

    # Celery发布失败会同步降级并已生成报告，避免再次调用模型。
    if not state.get("final_report"):
        report_stream_id = uuid.uuid4().hex
        await _send_json(ws, {
            "type": "agent_stream_start",
            "stream_id": report_stream_id,
            "operation": "force_finish",
            "status": "running",
            "message": "报告生成流程已启动",
        })
        try:
            from orchestration.nodes import (
                node_generate_report,
                node_study_plan,
                node_save_memory,
            )
            state = await asyncio.to_thread(node_generate_report, state)
            await _send_json(ws, {
                "type": "agent_status", "stream_id": report_stream_id,
                "operation": "force_finish", "node": "generate_report",
                "status": "completed", "message": "面试报告生成完成",
            })
            state = await asyncio.to_thread(node_study_plan, state)
            await _send_json(ws, {
                "type": "agent_status", "stream_id": report_stream_id,
                "operation": "force_finish", "node": "study_plan",
                "status": "completed", "message": "学习计划生成完成",
            })
            state = await asyncio.to_thread(node_save_memory, state)
            await _send_json(ws, {
                "type": "agent_status", "stream_id": report_stream_id,
                "operation": "force_finish", "node": "save_memory",
                "status": "completed", "message": "能力画像沉淀完成",
            })
            await _send_json(ws, {
                "type": "agent_stream_end", "stream_id": report_stream_id,
                "operation": "force_finish", "status": "completed",
            })
        except Exception as e:
            logger.error("强制结束生成报告失败: {}", e)
            await _send_json(ws, {
                "type": "agent_stream_end", "stream_id": report_stream_id,
                "operation": "force_finish", "status": "failed",
            })
            await _send_json(ws, {"type": "error", "message": f"生成报告失败: {e}"})
            return state

    memory_manager.save_session(session_id, state)

    fresh_w = await _get_fresh_long_term(
        state.get("user_id", "web_user"),
        list(state.get("long_term_weaknesses", []))
    )
    snapshot = _build_short_term_snapshot(state)
    snapshot["long_term_weaknesses"] = fresh_w

    farewell = "面试已结束，感谢你的参与！评估报告已生成，请查看。"
    text_stream_id = await _send_text_stream(ws, farewell, "finished")
    await _send_json(ws, {
        "type":                   "finished",
        "stream_id":              text_stream_id,
        "farewell":               farewell,
        "report":                 state.get("final_report", {}),
        "study_plan":             state.get("study_plan", {}),
        "github_recommendations": state.get("github_recommendations", []),
        "memory_snapshot":        snapshot,
    })
    await _send_tts(ws, farewell, voice=voice)
    return state


async def _handle_skill(
    ws: WebSocket,
    data: Dict[str, Any],
    state: Dict,
    graph,
) -> Dict:
    """处理技能调用"""
    session_id = state.get("session_id", "")
    skill_name = data.get("skill_name", "quiz")
    user_input = data.get("user_input", "")
    voice      = _get_voice(state)

    skill_state             = dict(state)
    skill_state["intent"]     = "use_skill"
    skill_state["skill_name"] = skill_name
    skill_state["user_input"] = user_input

    try:
        result = await _stream_graph(ws, graph, skill_state, "use_skill")
    except Exception as e:
        await _send_json(ws, {"type": "error", "message": f"技能调用失败: {e}"})
        return state

    reply = result.get("agent_reply", "")
    text_stream_id = await _send_text_stream(ws, reply, "skill_reply", {
        "skill_name": skill_name,
    })
    await _send_json(ws, {
        "type":       "skill_reply",
        "stream_id":  text_stream_id,
        "skill_name": skill_name,
        "text":       reply,
    })
    await _send_tts(ws, reply, voice=voice)

    state["skill_state"] = result.get("skill_state", {})
    memory_manager.save_session(session_id, state)
    return state


# ── WebSocket 主处理器 ────────────────────────────────────────────────────────

@router.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str):
    token = ws.query_params.get("token", "")
    try:
        current_user = decode_access_token(token)
        authenticated_user_id = str(current_user["sub"])
    except TokenError:
        await ws.close(code=1008, reason="Unauthorized")
        return

    await ws.accept()

    if session_id == "new":
        session_id = str(uuid.uuid4())
        await _send_json(ws, {"type": "session_id", "session_id": session_id})

    logger.info("WebSocket 连接建立: {}", session_id)

    graph      = _get_graph()
    state: Dict = memory_manager.load_session(session_id) or {}
    if state and state.get("user_id") != authenticated_user_id:
        await _send_json(ws, {"type": "error", "message": "无权访问该面试会话"})
        await ws.close(code=1008, reason="Forbidden")
        return
    task_event_forwarder = asyncio.create_task(
        _forward_task_events(ws, session_id),
        name=f"task-events:{session_id}",
    )
    _alive     = True

    try:
        while _alive:
            try:
                message = await ws.receive()
            except Exception:
                break

            # 连接断开
            if message.get("type") == "websocket.disconnect":
                _alive = False
                break

            # ── 二进制音频（STT）─────────────────────────────────────────
            if "bytes" in message and message["bytes"]:
                audio_bytes = message["bytes"]
                logger.debug("收到音频帧: {} bytes", len(audio_bytes))
                # ★ 只识别，结果回传给前端填入输入框，不自动提交
                await _handle_stt_audio(ws, audio_bytes)
                continue

            # ── 文本 JSON 消息 ────────────────────────────────────────────
            raw_text = message.get("text", "")
            if not raw_text:
                continue

            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                await _send_json(ws, {"type": "error", "message": "JSON 格式错误"})
                continue

            msg_type = data.get("type", "")

            # ping/pong 心跳
            if msg_type == "ping":
                await _send_json(ws, {"type": "pong"})

            # 启动面试
            elif msg_type == "start":
                result = await _handle_start(
                    ws, data, session_id, graph, authenticated_user_id
                )
                if result is not None:
                    state = result

            # 提交回答
            elif msg_type == "resume":
                await _handle_resume(ws, state)

            elif msg_type == "answer":
                if not state:
                    await _send_json(ws, {
                        "type": "error", "message": "请先发送 start 消息"
                    })
                    continue
                answer_text = data.get("text", "").strip()
                if not answer_text:
                    await _send_json(ws, {
                        "type": "error", "message": "回答不能为空"
                    })
                    continue
                state = await _handle_answer(ws, answer_text, state, graph)

            # 强制结束面试
            elif msg_type == "force_finish":
                if not state:
                    await _send_json(ws, {
                        "type": "error", "message": "请先发送 start 消息"
                    })
                    continue
                state = await _handle_force_finish(ws, state, graph)

            # 主动拉取记忆数据
            elif msg_type == "get_memory":
                user_id = authenticated_user_id
                if state:
                    await _send_memory_data(ws, state, user_id)
                else:
                    weaknesses = await _get_fresh_long_term(user_id, [])
                    await _send_json(ws, {
                        "type":       "memory_data",
                        "short_term": {
                            "qa_history": [],
                            "long_term_weaknesses": weaknesses,
                        },
                        "long_term": {"weaknesses": weaknesses},
                    })

            elif msg_type == "get_report_status":
                latest = memory_manager.load_session(session_id)
                if not latest or latest.get("user_id") != authenticated_user_id:
                    await _send_json(ws, {
                        "type": "error",
                        "message": "报告对应的面试会话不存在或无权访问",
                    })
                    continue

                state = latest
                task_id = str(state.get("report_task_id") or "")
                if task_id:
                    from memory.task_repository import background_task_repository

                    task_record = await asyncio.to_thread(
                        background_task_repository.get, task_id
                    )
                    if (
                        task_record
                        and task_record.get("user_id") == authenticated_user_id
                    ):
                        state["report_status"] = task_record.get("status", "")
                        state["report_stage"] = task_record.get("stage", "")
                        state["report_progress"] = task_record.get("progress", 0)
                report_status = state.get("report_status", "")
                if report_status == "completed" and state.get("final_report"):
                    fresh_w = await _get_fresh_long_term(
                        authenticated_user_id,
                        list(state.get("long_term_weaknesses", [])),
                    )
                    snapshot = _build_short_term_snapshot(state)
                    snapshot["long_term_weaknesses"] = fresh_w
                    farewell = "面试报告已生成，感谢你的参与！"
                    text_stream_id = await _send_text_stream(ws, farewell, "finished")
                    await _send_json(ws, {
                        "type": "finished",
                        "stream_id": text_stream_id,
                        "farewell": farewell,
                        "report": state.get("final_report", {}),
                        "study_plan": state.get("study_plan", {}),
                        "github_recommendations": state.get("github_recommendations", []),
                        "memory_snapshot": snapshot,
                    })
                    await _send_tts(ws, farewell, voice=_get_voice(state))
                elif report_status == "failed":
                    await _send_json(ws, {
                        "type": "report_failed",
                        "message": state.get("report_error") or "报告生成失败",
                    })
                else:
                    await _send_json(ws, {
                        "type": "report_status",
                        "status": report_status or "queued",
                        "task_id": state.get("report_task_id", ""),
                        "stage": state.get("report_stage", ""),
                        "progress": state.get("report_progress", 0),
                    })

            # 清除长期记忆
            elif msg_type == "clear_long_memory":
                user_id = authenticated_user_id
                ok = memory_manager.clear_weaknesses(user_id)
                if ok:
                    # 同步更新本地 state
                    if state:
                        state["long_term_weaknesses"] = []
                    # 推送更新后的记忆数据
                    snapshot = _build_short_term_snapshot(state) if state else {}
                    snapshot["long_term_weaknesses"] = []
                    await _send_json(ws, {
                        "type":       "memory_data",
                        "short_term": snapshot,
                        "long_term":  {"weaknesses": []},
                    })
                    await _send_json(ws, {
                        "type":  "toast",
                        "msg":   "长期记忆已清除 ✅",
                        "level": "success",
                    })
                else:
                    await _send_json(ws, {
                        "type":    "error",
                        "message": "清除失败，MySQL 可能不可用",
                    })

            # 查看 RAG 检索结果
            elif msg_type == "get_rag":
                if not state:
                    await _send_json(ws, {
                        "type": "error", "message": "请先开始面试"
                    })
                    continue
                rag_sources = state.get("rag_sources", [])
                rag_context = state.get("rag_context", "")
                await _send_json(ws, {
                    "type":        "rag_data",
                    "rag_sources": rag_sources[:10],
                    "rag_context": rag_context[:3000],
                    "total":       len(rag_sources),
                })

            # 手动朗读指定文本
            elif msg_type == "tts_speak":
                text_to_speak  = data.get("text", "").strip()
                voice_override = (data.get("voice") or
                                  _get_voice(state))
                logger.info(
                    "tts_speak: available={}, text_len={}, voice={}",
                    tts_engine.available,
                    len(text_to_speak),
                    voice_override,
                )
                await _send_tts(ws, text_to_speak, voice=voice_override)

            # Natural-language entry: let IntentRouter choose the business branch.
            elif msg_type == "route":
                user_text = data.get("text", "").strip()
                if not user_text:
                    await _send_json(ws, {"type": "error", "message": "输入不能为空"})
                    continue

                from agents.chat_agent import ChatAgent
                from agents.intent_router import IntentRouterAgent

                routed = await asyncio.to_thread(
                    IntentRouterAgent().run, {"user_input": user_text}
                )
                intent = routed.get("intent", "chat")
                skill_name = routed.get("skill_name", "")
                await _send_json(ws, {
                    "type": "intent_detected",
                    "intent": intent,
                    "confidence": routed.get("intent_confidence", 0.0),
                    "skill_name": skill_name,
                })

                if intent in ("start_interview", "input_jd"):
                    interview_topic = _extract_interview_topic(user_text)
                    start_data = {
                        "jd_text": _topic_as_minimal_jd(interview_topic),
                        "target_id": data.get("target_id"),
                        "interview_topic": interview_topic,
                        "resume_text": "",
                        "total_questions": int(data.get("total_questions", 5)),
                        "voice": data.get("voice"),
                    }
                    result = await _handle_start(
                        ws, start_data, session_id, graph, authenticated_user_id
                    )
                    if result is not None:
                        state = result
                elif intent == "answer_question":
                    if not state or not state.get("current_question_text"):
                        await _send_json(ws, {
                            "type": "error",
                            "message": "当前没有等待回答的面试题",
                        })
                    else:
                        state = await _handle_answer(ws, user_text, state, graph)
                elif intent == "use_skill":
                    if not state:
                        state = {
                            "session_id": session_id,
                            "user_id": authenticated_user_id,
                            "current_difficulty": "medium",
                            "skill_state": {},
                        }
                    state = await _handle_skill(ws, {
                        "skill_name": skill_name or "quiz",
                        "user_input": user_text,
                    }, state, graph)
                elif intent == "upload_resume":
                    await _send_json(ws, {
                        "type": "chat_reply",
                        "text": "请使用左侧的简历上传入口选择文件。",
                    })
                else:
                    chat_state = dict(state)
                    chat_state["user_input"] = user_text
                    agent_stream_id = uuid.uuid4().hex
                    await _send_json(ws, {
                        "type": "agent_stream_start",
                        "stream_id": agent_stream_id,
                        "operation": "chat",
                        "status": "running",
                        "message": "对话 Agent 已启动",
                    })
                    try:
                        chat_result = await asyncio.to_thread(ChatAgent().run, chat_state)
                    except Exception:
                        await _send_json(ws, {
                            "type": "agent_stream_end",
                            "stream_id": agent_stream_id,
                            "operation": "chat",
                            "status": "failed",
                        })
                        raise
                    await _send_json(ws, {
                        "type": "agent_status",
                        "stream_id": agent_stream_id,
                        "operation": "chat",
                        "node": "chat",
                        "status": "completed",
                        "message": "对话回复生成完成",
                    })
                    await _send_json(ws, {
                        "type": "agent_stream_end",
                        "stream_id": agent_stream_id,
                        "operation": "chat",
                        "status": "completed",
                    })
                    reply = chat_result.get("agent_reply", "")
                    text_stream_id = await _send_text_stream(ws, reply, "chat_reply")
                    await _send_json(ws, {
                        "type": "chat_reply", "stream_id": text_stream_id,
                        "text": reply,
                    })
                    await _send_tts(ws, reply, voice=data.get("voice"))

            # 技能调用
            elif msg_type == "skill":
                if not state:
                    await _send_json(ws, {
                        "type": "error", "message": "请先发送 start 消息"
                    })
                    continue
                state = await _handle_skill(ws, data, state, graph)

            else:
                await _send_json(ws, {
                    "type":    "error",
                    "message": f"未知消息类型: {msg_type}",
                })

    except WebSocketDisconnect:
        _alive = False
        logger.info("WebSocket 断开: {}", session_id)
    except Exception as e:
        _alive = False
        logger.error("WebSocket 异常: {} {}", session_id, e)
    finally:
        task_event_forwarder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task_event_forwarder
        if state:
            memory_manager.save_session(session_id, state)
        logger.info("WebSocket 连接关闭: {}", session_id)
