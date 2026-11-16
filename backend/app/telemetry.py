"""Prometheus metrics and OpenTelemetry wiring for the service layer.

The agent graph already creates spans and structured logs.  This module is the
missing half described in ``backend/agents/observability.py``: it configures a
TracerProvider + OTLP exporter and turns one finished ``DisputeResult`` trace
into the metrics required by D2/D5.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger("dispute.service.telemetry")

# --------------------------------------------------------------------------- #
# run metrics (recorded once, by the worker, when a run finishes)
# --------------------------------------------------------------------------- #
RUNS_TOTAL = Counter(
    "dispute_runs_total",
    "Finished runs by outcome, escalation reason and classification.",
    ("outcome", "reason_code", "classification"),
)
RUN_DURATION = Histogram(
    "dispute_run_duration_seconds",
    "Wall-clock time from worker dequeue to persisted result.",
    buckets=(1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 180, 300, 600),
)
RUN_STEPS = Histogram(
    "dispute_run_steps",
    "Trace entries per run (nodes, tools and model calls).",
    buckets=(1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 30, 50),
)
RUN_MODEL_CALLS = Histogram(
    "dispute_run_model_calls",
    "Model calls per run, including retries.",
    buckets=(1, 2, 3, 4, 5, 6, 8, 10, 14, 20),
)
RUN_TOOL_CALLS = Histogram(
    "dispute_run_tool_calls",
    "Tool calls per run.",
    buckets=(0, 1, 2, 3, 4, 5, 6, 8, 12, 16, 24),
)
STEP_LATENCY = Histogram(
    "dispute_step_latency_seconds",
    "Per-step latency from the result trace, labelled by step type and name.",
    ("type", "name"),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60),
)
RUN_RETRIES = Histogram(
    "dispute_run_retries",
    "Verifier-driven retries per run (attempts - 1).",
    buckets=(0, 1, 2, 3, 4, 5, 8),
)
RETRIES_TOTAL = Counter(
    "dispute_retries_total",
    "Retries by kind.",
    ("kind",),
)
TOKENS_TOTAL = Counter(
    "dispute_model_tokens_total",
    "Prompt and completion tokens recorded in the trace.",
    ("direction",),
)
COST_TOTAL = Counter(
    "dispute_model_cost_usd_total",
    "Model cost recorded in the trace (USD).",
)
ARCHIVE_FAILURES = Counter(
    "dispute_archive_failures_total",
    "Runs whose JSON archive could not be written (the Redis copy still exists).",
)
QUEUE_SUBMITTED = Counter(
    "dispute_queue_submitted_total",
    "Runs accepted by the API and enqueued.",
)
QUEUE_DEPTH = Gauge(
    "dispute_queue_depth",
    "Runs waiting in the Redis queue at the last scrape/sample.",
)
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled by the API.",
    ("method", "path", "status"),
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency.",
    ("method", "path"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)


def _get(entry: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a Pydantic TraceEntry or a stored dict."""
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _safe_name(value: Any) -> str:
    text = str(value or "unknown").strip() or "unknown"
    return text[:80]


def record_run_metrics(result: Any, duration_s: float) -> None:
    """Convert one ``DisputeResult`` into Prometheus observations."""
    status = str(getattr(result, "status", "") or "unknown")
    escalation = getattr(result, "escalation", None)
    reason_code = getattr(escalation, "reason_code", None) if escalation else None
    reason = getattr(reason_code, "value", reason_code) or ""
    classification = getattr(result, "classification", None) or ""

    RUNS_TOTAL.labels(outcome=status, reason_code=str(reason), classification=str(classification)).inc()
    RUN_DURATION.observe(max(0.0, float(duration_s or 0.0)))

    trace = list(getattr(result, "trace", None) or [])
    model_calls = sum(1 for e in trace if _get(e, "type") == "llm")
    tool_calls = sum(1 for e in trace if _get(e, "type") == "tool")
    RUN_STEPS.observe(len(trace))
    RUN_MODEL_CALLS.observe(model_calls)
    RUN_TOOL_CALLS.observe(tool_calls)

    total_prompt = total_completion = 0
    total_cost = 0.0
    for entry in trace:
        latency_ms = float(_get(entry, "latency_ms", 0.0) or 0.0)
        if latency_ms > 0:
            STEP_LATENCY.labels(
                type=_safe_name(_get(entry, "type", "unknown")),
                name=_safe_name(_get(entry, "name", "unknown")),
            ).observe(latency_ms / 1000.0)
        total_prompt += int(_get(entry, "prompt_tokens", 0) or 0)
        total_completion += int(_get(entry, "completion_tokens", 0) or 0)
        total_cost += float(_get(entry, "cost_usd", 0.0) or 0.0)

    if total_prompt:
        TOKENS_TOTAL.labels(direction="prompt").inc(total_prompt)
    if total_completion:
        TOKENS_TOTAL.labels(direction="completion").inc(total_completion)
    if total_cost:
        COST_TOTAL.inc(total_cost)

    verification = getattr(result, "verification", None)
    attempts = int(getattr(verification, "attempts", 0) or 0)
    retries = max(0, attempts - 1)
    RUN_RETRIES.observe(retries)
    if retries:
        RETRIES_TOTAL.labels(kind="verifier").inc(retries)


def record_run_failure(error_type: str) -> None:
    """A run that never produced a DisputeResult (infrastructure failure)."""
    RUNS_TOTAL.labels(
        outcome="failed", reason_code="infrastructure", classification="",
    ).inc()


def record_queue_depth(depth: int) -> None:
    QUEUE_DEPTH.set(max(0, int(depth)))


# --------------------------------------------------------------------------- #
# OpenTelemetry
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_configured = False
_provider: Any = None


def setup_tracing(settings: Any) -> bool:
    """Install the global TracerProvider once per process.

    Without ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or ``OTEL_CONSOLE=1``) spans are
    still created but dropped, which keeps tests and local runs quiet.
    """
    global _configured, _provider
    with _lock:
        if _configured:
            return True
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        except ImportError:  # pragma: no cover - deployment images install the SDK
            logger.warning("opentelemetry SDK not installed; spans will be no-ops")
            return False

        resource = Resource.create({
            "service.name": settings.service_name,
            "service.version": settings.service_version,
            "deployment.environment": settings.environment,
        })
        provider = TracerProvider(resource=resource)

        endpoint = (settings.otel_endpoint or "").strip()
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            except ImportError:  # pragma: no cover
                logger.warning("OTLP HTTP exporter not installed; spans will be dropped")
            else:
                if not endpoint.rstrip("/").endswith("/v1/traces"):
                    endpoint = endpoint.rstrip("/") + "/v1/traces"
                provider.add_span_processor(BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=endpoint, timeout=10),
                    schedule_delay_millis=2000,
                    max_export_batch_size=512,
                    export_timeout_millis=30000,
                ))
        elif settings.otel_console:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)
        _provider = provider
        _configured = True
        logger.info(
            "tracing configured service=%s endpoint=%s",
            settings.service_name, settings.otel_endpoint or "(none)",
        )
        return True


def force_flush(timeout_millis: int = 5000) -> None:
    if _provider is not None:
        try:
            _provider.force_flush(timeout_millis=timeout_millis)
        except Exception:  # noqa: BLE001 - telemetry must never fail a run
            logger.debug("tracing force_flush failed", exc_info=True)


def shutdown_tracing() -> None:
    global _configured, _provider
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception:  # noqa: BLE001
            logger.debug("tracing shutdown failed", exc_info=True)
    _provider = None
    _configured = False
