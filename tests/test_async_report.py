"""Tests for the optional Celery report path without invoking an LLM."""
from memory.task_repository import BackgroundTaskRepository
from orchestration.edges import route_after_evaluate
from orchestration.nodes import node_await_report
from orchestration.report_dispatch import dispatch_report


def test_finished_interview_uses_async_branch_when_enabled():
    assert route_after_evaluate({
        "interview_finished": True,
        "async_report": True,
    }) == "await_report"
    assert route_after_evaluate({
        "interview_finished": True,
        "async_report": False,
    }) == "generate_report"


def test_await_report_marks_state_pending():
    state = node_await_report({"interview_finished": True})
    assert state["report_status"] == "pending"


def test_dispatch_saves_before_publishing(monkeypatch):
    events = []

    def fake_save(session_id, state):
        events.append(("save", session_id, state["report_status"]))

    def fake_enqueue(session_id, task_id, *, headers=None):
        events.append(("publish", session_id, task_id, headers or {}))
        return task_id

    class FakeTaskRepository:
        def create_or_get(self, **kwargs):
            return {
                **kwargs, "status": "queued", "stage": "queued", "progress": 5
            }, True

        def update(self, task_id, **kwargs):
            return {"task_id": task_id, **kwargs}

    monkeypatch.setattr(
        "orchestration.report_dispatch.memory_manager.save_session",
        fake_save,
    )
    monkeypatch.setattr("tasks.report_tasks.enqueue_report", fake_enqueue)
    monkeypatch.setattr(
        "orchestration.report_dispatch.background_task_repository",
        FakeTaskRepository(),
    )
    monkeypatch.setattr(
        "orchestration.report_dispatch.publish_task_event",
        lambda **kwargs: "event-1",
    )

    state = dispatch_report({
        "session_id": "session-1",
        "async_report": True,
        "report_status": "pending",
        "runtime_trace_id": "trace-1",
    })

    assert state["report_status"] == "queued"
    assert state["report_task_id"]
    assert events[0] == ("save", "session-1", "queued")
    assert events[1][0:2] == ("publish", "session-1")
    assert events[1][3]["x-agent-trace-id"] == "trace-1"


def test_dispatch_does_not_publish_duplicate_active_task(monkeypatch):
    published = []

    class ExistingTaskRepository:
        def create_or_get(self, **kwargs):
            return {
                **kwargs, "status": "processing", "stage": "study_plan",
                "progress": 60,
            }, False

    monkeypatch.setattr(
        "orchestration.report_dispatch.background_task_repository",
        ExistingTaskRepository(),
    )
    monkeypatch.setattr(
        "orchestration.report_dispatch.memory_manager.save_session",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "tasks.report_tasks.enqueue_report",
        lambda *args, **kwargs: published.append(args),
    )

    state = dispatch_report({
        "session_id": "session-duplicate",
        "user_id": "user-1",
        "async_report": True,
        "report_status": "pending",
    })

    assert state["report_status"] == "processing"
    assert state["report_progress"] == 60
    assert published == []


def test_background_task_repository_is_idempotent_and_persistent(tmp_path):
    repository = BackgroundTaskRepository(
        f"sqlite:///{(tmp_path / 'tasks.sqlite3').as_posix()}"
    )
    first, created = repository.create_or_get(
        task_id="task-1",
        idempotency_key="report:session-1:v1",
        task_type="interview_report",
        session_id="session-1",
        user_id="user-1",
        trace_id="trace-1",
    )
    duplicate, duplicate_created = repository.create_or_get(
        task_id="task-2",
        idempotency_key="report:session-1:v1",
        task_type="interview_report",
        session_id="session-1",
        user_id="user-1",
    )
    completed = repository.update(
        first["task_id"], status="completed", stage="completed", progress=100
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate["task_id"] == "task-1"
    assert completed["status"] == "completed"
    assert repository.get("task-1")["progress"] == 100


def test_task_event_publisher_uses_pubsub_on_legacy_redis(monkeypatch):
    from tasks import event_bus

    calls = []

    class FakeRedis:
        def publish(self, channel, payload):
            calls.append((channel, payload))
            return 1

    monkeypatch.setattr(event_bus.settings, "task_event_stream_enabled", True)
    monkeypatch.setattr(event_bus.settings, "task_event_transport", "pubsub")
    monkeypatch.setattr(event_bus, "_sync_client", FakeRedis())

    result = event_bus.publish_task_event(
        session_id="session-1",
        task_id="task-1",
        status="processing",
        stage="study_plan",
        progress=60,
    )

    assert result == "1"
    assert calls[0][0].endswith(":session-1")
    assert '"progress": "60"' in calls[0][1]
