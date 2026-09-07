"""Low-cardinality Prometheus metrics shared by API and Celery processes."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST


HTTP_REQUESTS = Counter(
    "interviewagent_http_requests_total",
    "HTTP requests handled by the API.",
    ("method", "route", "status_class"),
)
HTTP_DURATION = Histogram(
    "interviewagent_http_request_duration_seconds",
    "HTTP request latency.",
    ("method", "route"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
DEPENDENCY_READY = Gauge(
    "interviewagent_dependency_ready",
    "Whether a readiness dependency passed its latest check.",
    ("dependency",),
)
AGENT_RUNS = Counter(
    "interviewagent_agent_runs_total",
    "Completed Agent runs.",
    ("operation", "outcome"),
)
AGENT_RUN_DURATION = Histogram(
    "interviewagent_agent_run_duration_seconds",
    "Agent run latency.",
    ("operation",),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 180, 300),
)
LLM_CALLS = Counter(
    "interviewagent_llm_calls_total",
    "LLM transport calls, including retry attempts.",
    ("model", "outcome"),
)
LLM_TOKENS = Counter(
    "interviewagent_llm_tokens_total",
    "Tokens reported by the LLM provider.",
    ("model", "direction"),
)
LLM_DURATION = Histogram(
    "interviewagent_llm_call_duration_seconds",
    "LLM call latency, including one transport attempt.",
    ("model",),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 45, 60, 90),
)
TOOL_CALLS = Counter(
    "interviewagent_tool_calls_total",
    "Tool calls finalized by the Tool Runtime.",
    ("tool", "outcome"),
)
TOOL_DURATION = Histogram(
    "interviewagent_tool_call_duration_seconds",
    "Tool call latency including bounded retries.",
    ("tool",),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
REPORT_TASKS = Counter(
    "interviewagent_report_tasks_total",
    "Finalized report tasks.",
    ("outcome", "execution"),
)
REPORT_RETRIES = Counter(
    "interviewagent_report_task_retries_total",
    "Report task retries requested by Celery.",
)
REPORT_DURATION = Histogram(
    "interviewagent_report_task_duration_seconds",
    "Queue-to-terminal report latency when durable state is available; otherwise execution latency.",
    ("execution",),
    buckets=(0.5, 1, 2.5, 5, 10, 20, 30, 60, 90, 120, 180, 300, 600),
)
REPORT_FALLBACKS = Counter(
    "interviewagent_report_fallbacks_total",
    "Reports switched from Celery dispatch to synchronous execution.",
    ("reason",),
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def record_agent_run(operation: str, outcome: str, duration_seconds: float) -> None:
    known_operations = {
        "start_interview", "input_jd", "upload_resume", "answer_question",
        "use_skill", "chat", "interview",
    }
    operation = operation if operation in known_operations else "unknown"
    AGENT_RUNS.labels(operation=operation, outcome=outcome).inc()
    AGENT_RUN_DURATION.labels(operation=operation).observe(
        max(0.0, duration_seconds)
    )


def record_llm_call(
    model: str,
    outcome: str,
    duration_seconds: float,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    model = model or "unknown"
    LLM_CALLS.labels(model=model, outcome=outcome).inc()
    LLM_DURATION.labels(model=model).observe(max(0.0, duration_seconds))
    if input_tokens > 0:
        LLM_TOKENS.labels(model=model, direction="input").inc(input_tokens)
    if output_tokens > 0:
        LLM_TOKENS.labels(model=model, direction="output").inc(output_tokens)


def record_tool_call(tool: str, ok: bool, duration_seconds: float) -> None:
    tool = tool or "unknown"
    TOOL_CALLS.labels(tool=tool, outcome="success" if ok else "failure").inc()
    TOOL_DURATION.labels(tool=tool).observe(max(0.0, duration_seconds))


def record_report_task(outcome: str, execution: str, duration_seconds: float) -> None:
    REPORT_TASKS.labels(outcome=outcome, execution=execution).inc()
    REPORT_DURATION.labels(execution=execution).observe(max(0.0, duration_seconds))
