"""AI planner for deadline-aware, actionable interview preparation."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any, Callable, Dict, List

from loguru import logger

from agents.base_agent import BaseAgent
from agents.output_schemas import LearningPlanDraft


SYSTEM_PROMPT = """\
你是面向求职者的技术面试学习规划师。请生成可执行的分阶段学习计划。

约束：
1. 所有阶段必须能在目标日期前完成，不安排目标日期之后的任务。
2. 根据每日可用分钟数控制任务量，每个任务必须有明确产出。
3. 优先覆盖 JD、重点方向、历史面试薄弱点。
4. 最后阶段应以模拟面试、错题复习和项目表达为主，不扩展大量新知识。
5. 如果是重新规划，不要重复已完成任务，重点处理未完成项和最新薄弱点。
6. 输出 phase 的 duration_days 和任务，不要输出具体日期，日期由系统统一计算。
7. phases 数组中只能放阶段对象；每个任务必须放在所属阶段的 tasks 数组中，
   绝对不能把任务直接放进 phases。

结构示例：
{
  "overall_strategy": "先补基础，再做项目训练，最后冲刺",
  "phases": [{
    "title": "基础阶段",
    "objective": "掌握核心概念",
    "duration_days": 7,
    "tasks": [{
      "title": "学习混合检索",
      "description": "完成笔记和代码示例",
      "knowledge_topic": "RAG",
      "estimated_minutes": 60
    }]
  }]
}
"""


def normalize_learning_plan_draft(raw: Dict[str, Any]) -> Dict[str, Any]:
    """修复模型常见的结构漂移，再交给 Pydantic 严格校验。

    Qwen 偶尔会把任务对象直接放进 phases。任务对象通常包含
    knowledge_topic 或 estimated_minutes，因此可以明确归入最近的阶段。
    """
    normalized: Dict[str, Any] = {
        "overall_strategy": str(raw.get("overall_strategy") or "按阶段完成面试准备"),
        "phases": [],
    }
    for item in raw.get("phases", []):
        if not isinstance(item, dict) or not item:
            continue
        is_task = (
            "knowledge_topic" in item or "estimated_minutes" in item
        ) and "tasks" not in item
        if is_task:
            if normalized["phases"] and item.get("title"):
                normalized["phases"][-1]["tasks"].append({
                    "title": str(item["title"]),
                    "description": str(item.get("description") or ""),
                    "knowledge_topic": str(item.get("knowledge_topic") or item["title"]),
                    "estimated_minutes": int(item.get("estimated_minutes") or 30),
                })
            continue

        tasks = item.get("tasks")
        if not isinstance(tasks, list):
            tasks = []
        clean_tasks = []
        for task in tasks:
            if not isinstance(task, dict) or not task.get("title"):
                continue
            clean_tasks.append({
                "title": str(task["title"]),
                "description": str(task.get("description") or ""),
                "knowledge_topic": str(task.get("knowledge_topic") or task["title"]),
                "estimated_minutes": int(task.get("estimated_minutes") or 30),
            })
        normalized["phases"].append({
            "title": str(item.get("title") or "学习阶段"),
            "objective": str(
                item.get("objective") or item.get("description") or "完成本阶段学习任务"
            ),
            "duration_days": int(item.get("duration_days") or 7),
            "tasks": clean_tasks,
        })

    normalized["phases"] = [
        phase for phase in normalized["phases"] if phase["tasks"]
    ]
    return normalized


class LearningPlanAgent(BaseAgent):
    name = "learning_plan"
    description = "按求职截止日期生成可执行的阶段学习任务"

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        prompt = json.dumps(state, ensure_ascii=False, default=str)
        raw = self.invoke_llm_json(
            SYSTEM_PROMPT,
            prompt,
        )
        normalized = normalize_learning_plan_draft(raw)
        state["learning_plan_draft"] = LearningPlanDraft.model_validate(
            normalized
        ).model_dump(mode="json")
        return state


def schedule_draft(
    draft: Dict[str, Any],
    *,
    start_date: date,
    target_date: date,
) -> List[Dict[str, Any]]:
    """Convert relative model output into dates while enforcing the deadline."""
    cursor = start_date
    scheduled: List[Dict[str, Any]] = []
    for phase_data in draft.get("phases", []):
        if cursor >= target_date:
            break
        requested = max(1, int(phase_data.get("duration_days", 1)))
        available = max(1, (target_date - cursor).days)
        duration = min(requested, available)
        phase_end = min(target_date - timedelta(days=1), cursor + timedelta(days=duration - 1))
        tasks = []
        span = max(1, (phase_end - cursor).days + 1)
        for index, task in enumerate(phase_data.get("tasks", [])):
            planned_date = cursor + timedelta(days=min(index, span - 1))
            tasks.append({
                "title": task["title"],
                "description": task.get("description", ""),
                "knowledge_topic": task["knowledge_topic"],
                "estimated_minutes": int(task["estimated_minutes"]),
                "planned_date": planned_date,
            })
        if tasks:
            scheduled.append({
                "title": phase_data["title"],
                "objective": phase_data["objective"],
                "start_date": cursor,
                "end_date": phase_end,
                "tasks": tasks,
            })
        cursor = phase_end + timedelta(days=1)
    if not scheduled:
        raise ValueError("模型没有生成可排期的学习任务")
    return scheduled


def _github_query(topic: str) -> str:
    """Add stable English keywords for common Chinese learning topics."""
    mappings = {
        "检索": "RAG retrieval",
        "向量": "vector search",
        "智能体": "AI agent",
        "代理": "AI agent",
        "算法": "algorithms",
        "系统设计": "system design",
        "数据结构": "data structures",
        "大模型": "large language model",
    }
    for keyword, query in mappings.items():
        if keyword in topic:
            return query
    return topic


def enrich_phases_with_resources(
    phases: List[Dict[str, Any]],
    *,
    search_fn: Callable[..., List[Dict[str, Any]]] | None = None,
    max_topics: int = 8,
) -> List[Dict[str, Any]]:
    """Use the real GitHub API tool and bind verified URLs to learning tasks.

    Resource discovery is best-effort: network/rate-limit failures never block
    plan generation. Topics are deduplicated and searched concurrently to keep
    latency bounded.
    """
    if search_fn is None:
        from tool_runtime import ToolContext, tool_executor

        def runtime_search(
            topic: str, max_results: int, min_stars: int
        ) -> List[Dict[str, Any]]:
            result = tool_executor.execute(
                "github.search_learning_repos",
                {
                    "topic": topic,
                    "max_results": max_results,
                    "min_stars": min_stars,
                },
                context=ToolContext.system_readonly(),
            )
            if not result.ok:
                logger.warning(
                    "学习资源工具调用失败：{} status={} error={}",
                    topic, result.status, result.error_code,
                )
                return []
            return list(result.data or [])

        search_fn = runtime_search

    topics = []
    for phase in phases:
        for task in phase["tasks"]:
            topic = str(task.get("knowledge_topic") or task["title"]).strip()
            if topic and topic not in topics and len(topics) < max_topics:
                topics.append(topic)

    found: Dict[str, List[Dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(topics)))) as pool:
        futures = {
            pool.submit(
                search_fn,
                _github_query(topic),
                max_results=2,
                min_stars=50,
            ): topic
            for topic in topics
        }
        for future in as_completed(futures):
            topic = futures[future]
            try:
                repos = future.result() or []
                found[topic] = [
                    {
                        "title": repo.get("name") or repo.get("url"),
                        "url": repo.get("url"),
                        "source_type": "github",
                        "description": repo.get("description") or "",
                        "stars": int(repo.get("stars") or 0),
                    }
                    for repo in repos
                    if str(repo.get("url") or "").startswith("https://github.com/")
                ]
            except Exception as exc:
                logger.warning("学习资源搜索失败：{} -> {}", topic, exc)
                found[topic] = []

    for phase in phases:
        for task in phase["tasks"]:
            topic = str(task.get("knowledge_topic") or task["title"]).strip()
            task["resources"] = found.get(topic, [])
    return phases
