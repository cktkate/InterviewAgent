"""Celery application configured with Redis as broker and result backend."""
from celery import Celery
from celery.signals import worker_ready
from loguru import logger
from prometheus_client import start_http_server

from config import settings


celery_app = Celery(
    "interview_agent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["tasks.report_tasks"],
)

celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=settings.celery_task_soft_time_limit_seconds,
    task_time_limit=settings.celery_task_time_limit_seconds,
    result_expires=86400,
)


_metrics_server_started = False


@worker_ready.connect
def start_worker_metrics(**_kwargs) -> None:
    """Expose worker-process metrics; the API process has its own /metrics."""
    global _metrics_server_started
    if _metrics_server_started or not settings.metrics_enabled:
        return
    port = int(settings.metrics_worker_port)
    if port <= 0:
        return
    try:
        start_http_server(port)
        _metrics_server_started = True
        logger.info("Celery Prometheus metrics listening on port {}", port)
    except OSError as exc:
        logger.warning("Celery metrics endpoint failed to start: {}", exc)
