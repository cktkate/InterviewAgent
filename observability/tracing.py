"""OpenTelemetry setup with a local, privacy-conscious JSONL exporter.

The JSONL trace is useful before an OTLP collector is deployed.  Span
attributes deliberately contain operational metadata only; prompts, resumes,
answers, tokens and credentials must never be attached here.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable, Optional

from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExportResult,
    SpanExporter,
)
from orchestration.security_policy import redact_observability_value


class JsonlSpanExporter(SpanExporter):
    """Export sanitized spans as one JSON object per line."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, spans: Iterable[ReadableSpan]) -> SpanExportResult:
        try:
            lines = [json.dumps(self._serialize(span), ensure_ascii=False) for span in spans]
            if not lines:
                return SpanExportResult.SUCCESS
            with self._lock, self.path.open("a", encoding="utf-8") as trace_file:
                trace_file.write("\n".join(lines) + "\n")
            return SpanExportResult.SUCCESS
        except Exception as exc:  # pragma: no cover - exporter must never break business calls
            logger.warning("Agent trace JSONL export failed: {}", exc)
            return SpanExportResult.FAILURE

    @staticmethod
    def _serialize(span: ReadableSpan) -> dict:
        context = span.context
        parent = span.parent
        duration_ms = None
        if span.start_time is not None and span.end_time is not None:
            duration_ms = round((span.end_time - span.start_time) / 1_000_000, 3)
        return {
            "trace_id": format(context.trace_id, "032x"),
            "span_id": format(context.span_id, "016x"),
            "parent_span_id": format(parent.span_id, "016x") if parent else None,
            "name": span.name,
            "status": span.status.status_code.name.lower(),
            "start_time_unix_nano": span.start_time,
            "duration_ms": duration_ms,
            "attributes": redact_observability_value(dict(span.attributes or {})),
            "events": [
                {
                    "name": event.name,
                    "timestamp_unix_nano": event.timestamp,
                    "attributes": redact_observability_value(dict(event.attributes or {})),
                }
                for event in span.events
            ],
        }


_configure_lock = threading.Lock()
_configured = False


def configure_tracing(
    *,
    enabled: bool,
    service_name: str,
    jsonl_enabled: bool,
    jsonl_path: str,
    otlp_endpoint: Optional[str] = None,
) -> None:
    """Configure the process-wide tracer provider once."""
    global _configured
    if not enabled or _configured:
        return

    with _configure_lock:
        if _configured:
            return

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if jsonl_enabled:
            provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(jsonl_path)))

        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
                )
            except Exception as exc:  # pragma: no cover - optional external backend
                logger.warning("OTLP trace exporter is unavailable: {}", exc)

        trace.set_tracer_provider(provider)
        _configured = True
        logger.info(
            "Agent tracing enabled: jsonl={}, otlp={}",
            jsonl_path if jsonl_enabled else "disabled",
            otlp_endpoint or "disabled",
        )


def get_tracer():
    return trace.get_tracer("interview_agent.runtime")
