from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter as PrometheusCounter
from prometheus_client import Histogram

logger = logging.getLogger("rag.events")
_LOGGING_CONFIGURED = False
COUNTERS: Counter[str] = Counter()
EVENT_COUNTER = PrometheusCounter("rag_events_total", "Application events", ["event"])
JOB_DURATION = Histogram(
    "rag_job_duration_seconds",
    "Worker job duration",
    ["job_type"],
    buckets=(0.1, 0.5, 1, 5, 15, 60, 300),
)

_SECRET_KEYS = re.compile(
    r"password|secret|token|authorization|cookie|credential|connection|string|prompt|content|query",
    re.I,
)
_ALLOWED_FIELDS = {
    "correlation_id",
    "job_id",
    "run_id",
    "conversation_id",
    "worker_id",
    "duration_ms",
    "retry_number",
    "final_status",
    "error_code",
    "error_type",
    "service",
    "status",
    "tenant_id",
}


def metric(name: str, amount: int = 1) -> None:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", name)[:80]
    COUNTERS[safe_name] += amount
    EVENT_COUNTER.labels(event=safe_name).inc(amount)


def configure_logging(level: str = "INFO") -> None:
    """Configure the application event logger to emit one JSON object per line."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    _LOGGING_CONFIGURED = True


def _safe(value: Any) -> Any:
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def log_event(event_type: str, **fields: Any) -> None:
    configure_logging()
    payload = {"event_type": re.sub(r"[^a-zA-Z0-9_.-]", "_", event_type)[:100]}
    for key, value in fields.items():
        if key in _ALLOWED_FIELDS and not _SECRET_KEYS.search(key):
            payload[key] = _safe(value)
    logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def configure_tracing(service_name: str, endpoint: str | None = None) -> None:
    """Configure local tracing; exporting is opt-in and never contains prompt content."""
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)


def tracer():
    return trace.get_tracer("rag-assistant")
