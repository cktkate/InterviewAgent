"""专项背诵题库、自评记录与复习调度。

该模块是正式面试之外的独立增量能力：运行时不调用 LLM，所有选题和复习时间
都由可测试的确定性规则控制。正式面试只通过 ``enqueue_interview_weaknesses``
单向写入弱点；写入失败不会影响面试主流程。
"""
from __future__ import annotations

import hashlib
import random
import re
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger
from sqlalchemy import create_engine, func, or_, select
from sqlalchemy.orm import sessionmaker

from config import settings
from memory.ability_repository import (
    GLOBAL_SCOPE,
    PRACTICE_OBSERVATIONS,
    PRACTICE_SELF_RATING_CONFIDENCE,
    apply_evidence_in_session,
    difficulty_score,
    normalize_scope,
)
from memory.time_utils import utc_now
from memory.models import (
    AbilityEvidence,
    Base,
    KnowledgeMastery,
    PracticeAttempt,
    PracticeQuestion,
    PracticeQuestionScope,
    PracticeReviewItem,
)


RATINGS = {"correct", "partial", "unknown"}
REVIEW_INTERVAL_DAYS = (1, 3, 7, 14, 30, 60)


BUILTIN_QUESTIONS: List[Dict[str, Any]] = [
    {
        "stem": "Python 中 GIL 是什么，它对多线程有什么影响？",
        "answer": "GIL 是 CPython 解释器中的全局解释器锁，同一进程内同一时刻通常只允许一个线程执行 Python 字节码。它简化了对象内存管理，但会限制 CPU 密集型 Python 多线程的并行能力；I/O 操作会释放 GIL，因此多线程仍适合 I/O 密集任务。CPU 密集任务通常选择多进程、原生扩展或能释放 GIL 的数值库。",
        "tech_stack": "python", "knowledge_point": "GIL", "difficulty": "medium",
        "keywords": ["CPython", "线程", "并行", "I/O密集", "多进程"],
    },
    {
        "stem": "Python 的生成器和普通函数有什么区别？",
        "answer": "包含 yield 的函数返回生成器对象，不会一次执行完并返回全部结果。每次迭代运行到 yield 后暂停，并保留局部变量和执行位置，下次继续执行。生成器采用惰性计算，适合流式处理和大数据集合，能够降低内存占用；代价是通常只能顺序消费，调试和重复遍历也更受限制。",
        "tech_stack": "python", "knowledge_point": "生成器", "difficulty": "basic",
        "keywords": ["yield", "惰性计算", "迭代器", "内存"],
    },
    {
        "stem": "FastAPI 中同步路由和异步路由应该如何选择？",
        "answer": "使用 async def 时，路由应调用真正的异步 I/O，例如异步数据库驱动和异步 HTTP 客户端，避免在事件循环中执行阻塞操作。普通 def 路由会由 FastAPI 放入线程池，适合无法异步化的阻塞库。CPU 密集任务不应长期占用事件循环或线程池，通常应交给进程池或任务队列。选择关键在于依赖调用链是否异步，而不是机械地全部写成 async。",
        "tech_stack": "fastapi", "knowledge_point": "异步并发", "difficulty": "medium",
        "keywords": ["async", "事件循环", "阻塞I/O", "线程池"],
    },
    {
        "stem": "Redis 为什么具有较高的读写性能？",
        "answer": "Redis 的核心数据主要保存在内存中，避免了磁盘随机 I/O；命令执行路径短并使用高效的数据结构；事件驱动和 I/O 多路复用能够处理大量连接；核心命令串行执行减少了锁竞争和上下文切换。Redis 6 以后可用 I/O 线程处理网络读写，但命令执行仍主要保持串行。性能也会受到大 key、慢命令、持久化和网络带宽影响。",
        "tech_stack": "redis", "knowledge_point": "Redis性能", "difficulty": "basic",
        "keywords": ["内存", "I/O多路复用", "事件驱动", "数据结构"],
    },
    {
        "stem": "什么是缓存穿透、缓存击穿和缓存雪崩？",
        "answer": "缓存穿透是持续查询缓存和数据库中都不存在的数据，可用参数校验、空值缓存和布隆过滤器缓解。缓存击穿是单个热点 key 失效后大量请求同时访问数据库，可用互斥重建、逻辑过期或热点永不过期缓解。缓存雪崩是大量 key 同时失效或缓存服务不可用，可通过过期时间加随机值、多级缓存、限流降级和高可用部署缓解。",
        "tech_stack": "redis", "knowledge_point": "缓存异常", "difficulty": "medium",
        "keywords": ["缓存穿透", "缓存击穿", "缓存雪崩", "布隆过滤器"],
    },
    {
        "stem": "MySQL 的联合索引为什么需要关注最左前缀原则？",
        "answer": "B+Tree 联合索引按索引列从左到右排序。查询只有利用左侧连续列，才能快速确定有序范围；跳过前导列时通常无法直接使用后续列完成有效定位。遇到范围条件后，后续列对缩小索引扫描范围的能力也可能受限。设计时应综合等值条件、范围条件、区分度、排序和覆盖索引需求，并使用 EXPLAIN 验证执行计划。",
        "tech_stack": "mysql", "knowledge_point": "联合索引", "difficulty": "medium",
        "keywords": ["B+Tree", "最左前缀", "范围查询", "EXPLAIN"],
    },
    {
        "stem": "RAG 系统为什么常把向量检索与 BM25 结合？",
        "answer": "向量检索擅长语义相似，能够召回表达不同但含义接近的内容；BM25 擅长精确关键词、缩写、专有名词和数字匹配。两者召回结果互补，通常可通过 RRF 等排名融合方法合并，再交给 reranker 精排。混合检索能降低单一路径的漏召回，但需要处理分块质量、去重、过滤、延迟和评测问题。",
        "tech_stack": "rag", "knowledge_point": "混合检索", "difficulty": "medium",
        "keywords": ["向量检索", "BM25", "RRF", "rerank"],
    },
    {
        "stem": "RAG 中为什么要进行文档分块，分块过大或过小分别有什么问题？",
        "answer": "分块是为了建立可检索的最小证据单元。分块过大容易混入无关信息、增加上下文和重排成本，并降低匹配精度；分块过小会丢失语义完整性和跨句关系，导致答案缺少必要上下文。实践中应根据文档结构按标题、段落或语义边界切分，保留来源与层级元数据，并通过召回率、MRR 和答案正确性实验确定大小与重叠。",
        "tech_stack": "rag", "knowledge_point": "文档分块", "difficulty": "medium",
        "keywords": ["chunk", "语义边界", "上下文", "召回率"],
    },
    {
        "stem": "AI Agent 中规划、工具调用和记忆分别解决什么问题？",
        "answer": "规划负责把目标拆成步骤并决定执行顺序；工具调用让模型通过受控接口读取外部信息或改变环境；记忆保存当前任务状态、历史事实和用户偏好，使跨步骤与跨会话决策保持连续。工程实现还需要权限确认、参数校验、超时重试、幂等、Trace 和失败恢复，不能把所有控制权都交给模型自由生成。",
        "tech_stack": "agent", "knowledge_point": "Agent架构", "difficulty": "basic",
        "keywords": ["规划", "工具调用", "记忆", "安全"],
    },
    {
        "stem": "为什么 Agent 的流程决策适合使用确定性策略，而不是完全交给大模型？",
        "answer": "大模型输出具有随机性，容易出现越权调用、循环、遗漏约束和难以复现的问题。确定性策略可负责状态机、权限、预算、重试、停止条件与路由，大模型负责理解、生成和软判断。二者结合能获得灵活性，同时保证流程可审计、可测试和可恢复；关键决策还应记录输入信号、规则结果和 Trace。",
        "tech_stack": "agent", "knowledge_point": "Agent控制策略", "difficulty": "medium",
        "keywords": ["确定性策略", "状态机", "可审计", "可测试"],
    },
    {
        "stem": "大模型结构化输出为什么仍然需要本地校验和降级处理？",
        "answer": "即使提示模型输出 JSON，也可能出现字段缺失、类型错误、枚举越界、额外文本或截断。工程上应使用 JSON Schema 或 Pydantic 校验，必要时有限次数重试，并准备规则解析、默认值或跳过当前任务等降级路径。结构化校验失败不应无限重试，也不应让非关键能力拖垮整个请求。",
        "tech_stack": "llm", "knowledge_point": "结构化输出", "difficulty": "medium",
        "keywords": ["JSON Schema", "Pydantic", "重试", "降级"],
    },
    {
        "stem": "WebSocket 与普通 HTTP 请求相比适合什么场景？",
        "answer": "HTTP 通常是一问一答的无状态请求，适合 CRUD 和短请求。WebSocket 建立长连接后支持客户端与服务端双向主动推送，适合实时聊天、语音流、进度事件和协作状态。使用时需要处理鉴权、心跳、断线重连、背压、消息顺序、连接资源以及多实例部署下的广播和会话共享。",
        "tech_stack": "backend", "knowledge_point": "WebSocket", "difficulty": "basic",
        "keywords": ["长连接", "双向通信", "心跳", "断线重连"],
    },
]


def _fingerprint(stem: str) -> str:
    normalized = re.sub(r"\W+", "", stem.lower(), flags=re.UNICODE)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class PracticeRepository:
    def __init__(self) -> None:
        self._engine = None
        self._SessionLocal = None
        self._ready_lock = threading.Lock()
        self._seeded = False

    def _ensure_ready(self) -> None:
        if self._SessionLocal is not None and self._seeded:
            return
        # 首页会并行请求 overview/catalog；加锁避免空库首次启动时重复播种。
        with self._ready_lock:
            if self._SessionLocal is None:
                self._engine = create_engine(
                    settings.mysql_url,
                    pool_pre_ping=True,
                    pool_size=5,
                    max_overflow=10,
                )
                self._SessionLocal = sessionmaker(bind=self._engine, expire_on_commit=False)
                Base.metadata.create_all(
                    self._engine,
                    tables=[
                        PracticeQuestion.__table__,
                        PracticeAttempt.__table__,
                        PracticeReviewItem.__table__,
                        PracticeQuestionScope.__table__,
                        KnowledgeMastery.__table__,
                        AbilityEvidence.__table__,
                    ],
                )
            if not self._seeded:
                self.seed_builtin_questions()
                self._seeded = True

    def seed_builtin_questions(self) -> int:
        """幂等写入内置题库，已有相同指纹时跳过。"""
        if self._SessionLocal is None:
            return 0
        inserted = 0
        with self._SessionLocal() as session:
            existing = set(session.scalars(
                select(PracticeQuestion.fingerprint).where(
                    PracticeQuestion.owner_user_id.is_(None),
                    PracticeQuestion.origin == "builtin",
                )
            ).all())
            for raw in BUILTIN_QUESTIONS:
                fingerprint = _fingerprint(raw["stem"])
                if fingerprint in existing:
                    continue
                session.add(PracticeQuestion(
                    id=str(uuid.uuid4()),
                    owner_user_id=None,
                    fingerprint=fingerprint,
                    origin="builtin",
                    **raw,
                ))
                existing.add(fingerprint)
                inserted += 1
            session.commit()
        return inserted

    @staticmethod
    def _question_dict(question: PracticeQuestion, include_answer: bool = False) -> Dict[str, Any]:
        result = {
            "id": question.id,
            "stem": question.stem,
            "tech_stack": question.tech_stack,
            "knowledge_point": question.knowledge_point,
            "difficulty": question.difficulty,
            "keywords": list(question.keywords or []),
            "origin": question.origin,
        }
        if include_answer:
            result["answer"] = question.answer
        return result

    @staticmethod
    def _review_dict(item: PracticeReviewItem) -> Dict[str, Any]:
        return {
            "id": item.id,
            "question_id": item.question_id,
            "source": item.source,
            "level": item.level,
            "attempts": item.attempts,
            "correct_streak": item.correct_streak,
            "last_rating": item.last_rating,
            "status": item.status,
            "due_at": item.due_at,
        }

    @staticmethod
    def _visible_question_filter(user_id: str):
        return or_(
            PracticeQuestion.owner_user_id.is_(None),
            PracticeQuestion.owner_user_id == user_id,
        )

    def catalog(self, user_id: str) -> Dict[str, Any]:
        self._ensure_ready()
        with self._SessionLocal() as session:
            questions = list(session.scalars(
                select(PracticeQuestion).where(
                    PracticeQuestion.is_active.is_(True),
                    self._visible_question_filter(user_id),
                ).order_by(PracticeQuestion.tech_stack, PracticeQuestion.knowledge_point)
            ).all())
        stacks: Dict[str, Dict[str, Any]] = {}
        for question in questions:
            stack = stacks.setdefault(question.tech_stack, {"name": question.tech_stack, "count": 0, "knowledge_points": {}})
            stack["count"] += 1
            points = stack["knowledge_points"]
            points[question.knowledge_point] = points.get(question.knowledge_point, 0) + 1
        return {
            "total_questions": len(questions),
            "tech_stacks": [
                {
                    "name": value["name"],
                    "count": value["count"],
                    "knowledge_points": [
                        {"name": name, "count": count}
                        for name, count in sorted(value["knowledge_points"].items())
                    ],
                }
                for _, value in sorted(stacks.items())
            ],
        }

    def overview(self, user_id: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        self._ensure_ready()
        now = now or utc_now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._SessionLocal() as session:
            total_questions = session.scalar(select(func.count()).select_from(PracticeQuestion).where(
                PracticeQuestion.is_active.is_(True), self._visible_question_filter(user_id)
            )) or 0
            due_count = session.scalar(select(func.count()).select_from(PracticeReviewItem).where(
                PracticeReviewItem.user_id == user_id,
                PracticeReviewItem.status == "active",
                PracticeReviewItem.due_at <= now,
            )) or 0
            interview_count = session.scalar(select(func.count()).select_from(PracticeReviewItem).where(
                PracticeReviewItem.user_id == user_id,
                PracticeReviewItem.status == "active",
                PracticeReviewItem.source == "interview",
            )) or 0
            today_attempts = session.scalar(select(func.count()).select_from(PracticeAttempt).where(
                PracticeAttempt.user_id == user_id,
                PracticeAttempt.created_at >= today,
            )) or 0
            mastered_today = session.scalar(select(func.count()).select_from(PracticeAttempt).where(
                PracticeAttempt.user_id == user_id,
                PracticeAttempt.created_at >= today,
                PracticeAttempt.rating == "correct",
            )) or 0
        return {
            "total_questions": int(total_questions),
            "due_count": int(due_count),
            "interview_weak_count": int(interview_count),
            "today_attempts": int(today_attempts),
            "today_correct": int(mastered_today),
        }

    def next_question(
        self,
        user_id: str,
        mode: str = "category",
        tech_stack: str = "",
        knowledge_point: str = "",
        exclude_ids: Optional[Iterable[str]] = None,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """返回题干，不返回答案；答案必须由 reveal 接口单独获取。"""
        self._ensure_ready()
        now = now or utc_now()
        excluded = {str(item) for item in (exclude_ids or [])}
        with self._SessionLocal() as session:
            if mode in {"due", "interview"}:
                statement = (
                    select(PracticeQuestion, PracticeReviewItem)
                    .join(PracticeReviewItem, PracticeReviewItem.question_id == PracticeQuestion.id)
                    .where(
                        PracticeReviewItem.user_id == user_id,
                        PracticeReviewItem.status == "active",
                        PracticeQuestion.is_active.is_(True),
                        self._visible_question_filter(user_id),
                    )
                )
                if mode == "due":
                    statement = statement.where(PracticeReviewItem.due_at <= now)
                else:
                    statement = statement.where(PracticeReviewItem.source == "interview")
                if tech_stack:
                    statement = statement.where(PracticeQuestion.tech_stack == tech_stack)
                if knowledge_point:
                    statement = statement.where(PracticeQuestion.knowledge_point == knowledge_point)
                rows = list(session.execute(statement.order_by(PracticeReviewItem.due_at)).all())
                candidates = [q for q, _ in rows if q.id not in excluded]
            else:
                statement = select(PracticeQuestion).where(
                    PracticeQuestion.is_active.is_(True),
                    self._visible_question_filter(user_id),
                )
                if tech_stack:
                    statement = statement.where(PracticeQuestion.tech_stack == tech_stack)
                if knowledge_point:
                    statement = statement.where(PracticeQuestion.knowledge_point == knowledge_point)
                candidates = [
                    q for q in session.scalars(statement).all() if q.id not in excluded
                ]
            if not candidates:
                return None
            return self._question_dict(random.SystemRandom().choice(candidates))

    def reveal_answer(self, user_id: str, question_id: str) -> Optional[Dict[str, Any]]:
        self._ensure_ready()
        with self._SessionLocal() as session:
            question = session.execute(select(PracticeQuestion).where(
                PracticeQuestion.id == question_id,
                PracticeQuestion.is_active.is_(True),
                self._visible_question_filter(user_id),
            )).scalar_one_or_none()
            return self._question_dict(question, include_answer=True) if question else None

    def record_attempt(
        self,
        user_id: str,
        question_id: str,
        practice_session_id: str,
        rating: str,
        spoken_answer: str = "",
        source: str = "practice",
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        if rating not in RATINGS:
            raise ValueError("invalid practice rating")
        self._ensure_ready()
        now = now or utc_now()
        with self._SessionLocal() as session:
            question = session.execute(select(PracticeQuestion).where(
                PracticeQuestion.id == question_id,
                PracticeQuestion.is_active.is_(True),
                self._visible_question_filter(user_id),
            )).scalar_one_or_none()
            if question is None:
                return None
            attempt_id = str(uuid.uuid4())
            session.add(PracticeAttempt(
                id=attempt_id,
                practice_session_id=practice_session_id,
                user_id=user_id,
                question_id=question_id,
                rating=rating,
                spoken_answer=spoken_answer.strip(),
                source=source,
                created_at=now,
            ))
            item = session.execute(select(PracticeReviewItem).where(
                PracticeReviewItem.user_id == user_id,
                PracticeReviewItem.question_id == question_id,
            )).scalar_one_or_none()
            if item is None:
                item = PracticeReviewItem(
                    id=str(uuid.uuid4()), user_id=user_id, question_id=question_id,
                    source=source, due_at=now,
                )
                session.add(item)
            item.attempts = int(item.attempts or 0) + 1
            item.last_rating = rating
            item.last_reviewed_at = now
            item.status = "active"
            if rating == "correct":
                item.level = min(len(REVIEW_INTERVAL_DAYS), int(item.level or 0) + 1)
                item.correct_streak = int(item.correct_streak or 0) + 1
                item.due_at = now + timedelta(days=REVIEW_INTERVAL_DAYS[item.level - 1])
            elif rating == "partial":
                item.correct_streak = 0
                item.due_at = now + timedelta(days=1)
            else:
                item.level = 0
                item.correct_streak = 0
                item.due_at = now + timedelta(minutes=10)

            # Self-rating is deliberately a weak prior.  It updates the same
            # cross-session profile as formal interviews, but its aggregate
            # contribution is capped at 25% by the ability repository.
            scope_row = session.get(PracticeQuestionScope, question_id)
            target_scope = normalize_scope(
                scope_row.target_scope if scope_row is not None else GLOBAL_SCOPE
            )
            try:
                with session.begin_nested():
                    apply_evidence_in_session(
                        session,
                        evidence_id=hashlib.sha256(
                            f"practice:{attempt_id}".encode("utf-8")
                        ).hexdigest(),
                        user_id=user_id,
                        target_scope=target_scope,
                        knowledge_point=question.knowledge_point,
                        source_type="practice",
                        evidence_kind=f"practice_{rating}",
                        observation=PRACTICE_OBSERVATIONS[rating],
                        difficulty=difficulty_score(question.difficulty),
                        confidence=PRACTICE_SELF_RATING_CONFIDENCE,
                        session_id=practice_session_id,
                        question_id=question_id,
                        question=question.stem,
                        answer=spoken_answer.strip(),
                        details={"rating": rating, "origin": question.origin},
                        created_at=now,
                    )
            except Exception as exc:
                # A profile update is auxiliary and must not discard a valid
                # practice attempt or its deterministic review schedule.
                logger.warning("专项练习能力画像更新失败，已降级：{}", exc)
            session.commit()
            session.refresh(item)
            return self._review_dict(item)

    def enqueue_interview_weaknesses(
        self, state: Dict[str, Any], now: Optional[datetime] = None
    ) -> int:
        """把未达标的主问题幂等加入用户私有题库和复习队列。"""
        user_id = str(state.get("user_id") or "")
        session_id = str(state.get("session_id") or "")
        if not user_id:
            return 0
        self._ensure_ready()
        now = now or utc_now()
        plan_by_id = {
            str(item.get("id")): item for item in (state.get("question_plan") or [])
        }
        queued = 0
        with self._SessionLocal() as session:
            for record in state.get("score_records") or []:
                if record.get("is_followup"):
                    continue
                score = record.get("score") or {}
                if bool(score.get("is_correct", False)):
                    continue
                stem = str(record.get("question") or "").strip()
                if not stem:
                    continue
                planned = plan_by_id.get(str(record.get("question_id")), {})
                expected = [str(x).strip() for x in (planned.get("expected_points") or []) if str(x).strip()]
                if not expected:
                    expected = [str(x).strip() for x in (score.get("key_missing") or []) if str(x).strip()]
                if not expected:
                    continue
                fingerprint = _fingerprint(stem)
                question = session.execute(select(PracticeQuestion).where(
                    PracticeQuestion.owner_user_id == user_id,
                    PracticeQuestion.fingerprint == fingerprint,
                )).scalar_one_or_none()
                if question is None:
                    question = PracticeQuestion(
                        id=str(uuid.uuid4()), owner_user_id=user_id,
                        fingerprint=fingerprint, stem=stem,
                        answer="参考要点：\n" + "\n".join(f"{i}. {point}" for i, point in enumerate(expected, 1)),
                        tech_stack=str(planned.get("topic") or "interview"),
                        knowledge_point=str(record.get("knowledge_point") or planned.get("knowledge_point") or "面试弱点"),
                        difficulty=str(planned.get("difficulty") or "medium"),
                        keywords=expected[:6], origin="interview",
                        source_session_id=session_id or None,
                    )
                    session.add(question)
                    session.flush()
                target_scope = normalize_scope(str(state.get("target_id") or ""))
                scope_row = session.get(PracticeQuestionScope, question.id)
                if scope_row is None:
                    session.add(PracticeQuestionScope(
                        question_id=question.id,
                        user_id=user_id,
                        target_scope=target_scope,
                        created_at=now,
                    ))
                elif target_scope != GLOBAL_SCOPE:
                    scope_row.target_scope = target_scope
                item = session.execute(select(PracticeReviewItem).where(
                    PracticeReviewItem.user_id == user_id,
                    PracticeReviewItem.question_id == question.id,
                )).scalar_one_or_none()
                if item is None:
                    session.add(PracticeReviewItem(
                        id=str(uuid.uuid4()), user_id=user_id,
                        question_id=question.id, source="interview",
                        source_session_id=session_id or None, due_at=now,
                    ))
                    queued += 1
                else:
                    item.source = "interview"
                    item.source_session_id = session_id or item.source_session_id
                    if item.due_at > now:
                        item.due_at = now
            session.commit()
        return queued


practice_repository = PracticeRepository()
