"""Production health probes and Prometheus exposition tests."""
from __future__ import annotations

from fastapi.testclient import TestClient

from observability.health import DependencyCheck
from observability.metrics import record_llm_call, record_report_task, record_tool_call


def test_liveness_and_startup_probes() -> None:
    from api.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        live = client.get("/health/live")
        startup = client.get("/health/startup")

    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert startup.status_code == 200
    assert startup.json()["status"] == "ok"


def test_startup_probe_fails_before_lifespan() -> None:
    from api.main import app

    app.state.startup_complete = False
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/startupz")
    assert response.status_code == 503
    assert response.json()["status"] == "starting"


def test_readiness_reports_required_dependency_failure(monkeypatch) -> None:
    from api.main import app
    from api.routers import health as health_router

    async def fake_readiness():
        return {
            "mysql": DependencyCheck("up", 1.0),
            "redis": DependencyCheck("down", 2.0, reason="TimeoutError"),
            "weaviate": DependencyCheck("up", 3.0),
            "celery": DependencyCheck("disabled", 0.0, False, "CELERY_ENABLED=false"),
        }

    monkeypatch.setattr(health_router, "check_readiness", fake_readiness)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"]["redis"]["reason"] == "TimeoutError"
    assert body["dependencies"]["celery"]["required"] is False


def test_metrics_endpoint_is_prometheus_compatible() -> None:
    from api.main import app

    record_llm_call("test-model", "success", 0.01, input_tokens=3, output_tokens=2)
    record_tool_call("test.tool", True, 0.02)
    record_report_task("success", "test", 0.03)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "interviewagent_llm_tokens_total" in response.text
    assert "interviewagent_tool_calls_total" in response.text
    assert "interviewagent_report_task_duration_seconds_bucket" in response.text
    assert "interviewagent_http_requests_total" in response.text
