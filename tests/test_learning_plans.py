"""学习计划排期、状态流转和重新规划测试。"""
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agents.learning_plan import (
    enrich_phases_with_resources,
    normalize_learning_plan_draft,
    schedule_draft,
)
from agents.output_schemas import LearningPlanDraft
from memory.learning_plan_repository import LearningPlanRepository
from memory.models import (
    LearningPhase,
    LearningPlan,
    LearningTask,
    LearningTaskResource,
)


def _repository() -> LearningPlanRepository:
    repository = LearningPlanRepository()
    repository._engine = create_engine("sqlite+pysqlite:///:memory:")
    repository._SessionLocal = sessionmaker(
        bind=repository._engine, expire_on_commit=False
    )
    LearningPlan.__table__.create(repository._engine)
    LearningPhase.__table__.create(repository._engine)
    LearningTask.__table__.create(repository._engine)
    LearningTaskResource.__table__.create(repository._engine)
    return repository


def _phases(prefix: str):
    today = date.today()
    return [{
        "title": prefix + "阶段",
        "objective": "完成阶段目标",
        "start_date": today,
        "end_date": today + timedelta(days=2),
        "tasks": [
            {
                "title": prefix + "任务A",
                "description": "学习并输出笔记",
                "knowledge_topic": "RAG",
                "planned_date": today,
                "estimated_minutes": 60,
                "resources": [{
                    "title": "example/repo",
                    "url": "https://github.com/example/repo",
                    "source_type": "github",
                    "description": "示例资源",
                    "stars": 200,
                }],
            },
            {
                "title": prefix + "任务B",
                "description": "完成专项练习",
                "knowledge_topic": "Agent",
                "planned_date": today + timedelta(days=1),
                "estimated_minutes": 45,
            },
        ],
    }]


def test_schedule_never_exceeds_target_date():
    today = date.today()
    target = today + timedelta(days=3)
    draft = {
        "phases": [{
            "title": "基础",
            "objective": "补齐基础",
            "duration_days": 20,
            "tasks": [
                {
                    "title": "学习RAG",
                    "description": "",
                    "knowledge_topic": "RAG",
                    "estimated_minutes": 60,
                }
            ],
        }]
    }
    phases = schedule_draft(draft, start_date=today, target_date=target)
    assert phases[0]["end_date"] < target
    assert all(task["planned_date"] < target for task in phases[0]["tasks"])


def test_normalize_moves_flattened_tasks_back_to_latest_phase():
    raw = {
        "overall_strategy": "先基础后实战",
        "phases": [
            {
                "title": "基础知识",
                "objective": "掌握Python",
                "duration_days": 7,
                "tasks": "malformed",
            },
            {
                "title": "Python语法",
                "description": "完成语法复习",
                "knowledge_topic": "Python",
                "estimated_minutes": 60,
            },
            {},
        ],
    }
    normalized = normalize_learning_plan_draft(raw)
    assert len(normalized["phases"]) == 1
    assert normalized["phases"][0]["tasks"][0]["title"] == "Python语法"
    assert LearningPlanDraft.model_validate(normalized)


def test_resource_enrichment_uses_only_real_github_results():
    phases = _phases("资源")

    def fake_search(topic, max_results, min_stars):
        return [
            {
                "name": "official/example",
                "url": "https://github.com/official/example",
                "description": "学习示例",
                "stars": 999,
            },
            {
                "name": "invalid",
                "url": "javascript:alert(1)",
                "description": "",
                "stars": 0,
            },
        ]

    enriched = enrich_phases_with_resources(phases, search_fn=fake_search)
    resources = enriched[0]["tasks"][0]["resources"]
    assert len(resources) == 1
    assert resources[0]["url"] == "https://github.com/official/example"


def test_task_status_and_replan_preserve_completed_tasks():
    repository = _repository()
    target_date = date.today() + timedelta(days=20)
    initial = repository.save_generated(
        target_id="target-1",
        user_id="user-1",
        target_date=target_date,
        daily_minutes=60,
        days_per_week=6,
        reason="initial",
        phases=_phases("旧"),
        replace_future=False,
    )
    completed_task = initial["phases"][0]["tasks"][0]
    pending_task = initial["phases"][0]["tasks"][1]
    repository.update_task(
        completed_task["id"], "user-1", status="completed", user_note="已掌握"
    )
    assert repository.update_task(pending_task["id"], "user-2", status="completed") is None

    replanned = repository.save_generated(
        target_id="target-1",
        user_id="user-1",
        target_date=target_date,
        daily_minutes=90,
        days_per_week=5,
        reason="阶段结束",
        phases=_phases("新"),
        replace_future=True,
    )
    task_titles = [
        task["title"] for phase in replanned["phases"] for task in phase["tasks"]
    ]
    assert "旧任务A" in task_titles
    assert "旧任务B" not in task_titles
    assert "新任务A" in task_titles
    assert replanned["completed_tasks"] == 1
    assert replanned["version"] == 2
    completed_resources = next(
        task["resources"]
        for phase in replanned["phases"]
        for task in phase["tasks"]
        if task["title"] == "旧任务A"
    )
    assert completed_resources[0]["url"] == "https://github.com/example/repo"


def test_learning_plan_isolated_by_user():
    repository = _repository()
    repository.save_generated(
        target_id="target-1",
        user_id="user-1",
        target_date=date.today() + timedelta(days=10),
        daily_minutes=60,
        days_per_week=6,
        reason="initial",
        phases=_phases("用户一"),
        replace_future=False,
    )
    assert repository.get_for_target("target-1", "user-2") is None
