"""求职目标仓储与面试关联测试。"""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from memory.job_target_repository import JobTargetRepository
from memory.models import JobTarget, JobTargetSession


def _repository() -> JobTargetRepository:
    repository = JobTargetRepository()
    repository._engine = create_engine("sqlite+pysqlite:///:memory:")
    repository._SessionLocal = sessionmaker(
        bind=repository._engine,
        expire_on_commit=False,
    )
    JobTarget.__table__.create(repository._engine)
    JobTargetSession.__table__.create(repository._engine)
    return repository


def test_job_target_crud_and_user_isolation():
    repository = _repository()
    target = repository.create("user-1", {
        "title": "AI 应用开发秋招",
        "company_name": "Example",
        "position_name": "AI 应用开发工程师",
        "jd_text": "要求掌握 RAG 和 Agent",
        "target_date": date(2026, 9, 20),
        "focus_areas": ["RAG", "Agent"],
    })

    assert repository.get_for_user(target["id"], "user-2") is None
    assert repository.list_for_user("user-1")[0]["position_name"] == "AI 应用开发工程师"

    updated = repository.update(
        target["id"], "user-1", {"status": "interviewing"}
    )
    assert updated["status"] == "interviewing"

    archived = repository.archive(target["id"], "user-1")
    assert archived["status"] == "archived"
    assert repository.list_for_user("user-1") == []
    assert len(repository.list_for_user("user-1", include_archived=True)) == 1


def test_job_target_can_link_existing_session_without_changing_old_record_table():
    repository = _repository()
    target = repository.create("user-1", {
        "title": "Agent 开发",
        "position_name": "Agent 开发工程师",
    })

    assert repository.link_session(target["id"], "session-1", "user-1") is True

    with repository._SessionLocal() as session:
        link = session.get(JobTargetSession, "session-1")
        assert link.target_id == target["id"]
        assert link.user_id == "user-1"
