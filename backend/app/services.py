"""Application services: submit, enqueue, execute and persist one dispute run.

The API and the worker share this module so the same run-state transitions are
used in tests and in production.  The graph itself is imported lazily so the
API process does not pay for LangGraph/retrieval at import time.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
import uuid
from typing import Any, Callable

from backend.app.config import Settings
from backend.app.schemas import DisputeRequest, RunSummary
from backend.app.storage import (
    RunRecord,
    RunStore,
    archive_record,
    build_store,
    utcnow,
)
from backend.app.telemetry import (
    ARCHIVE_FAILURES,
    QUEUE_SUBMITTED,
    force_flush,
    record_queue_depth,
    record_run_failure,
    record_run_metrics,
)

logger = logging.getLogger("dispute.service")

_CORRELATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_IDEMPOTENCY_RE = re.compile(r"^[\x21-\x7e]{1,200}$")


class QueueUnavailable(RuntimeError):
    """The API accepted the request but could not enqueue it."""


class StoreUnavailable(RuntimeError):
    """The run store could not be reached."""


# --------------------------------------------------------------------------- #
# enqueue
# --------------------------------------------------------------------------- #
class RedisEnqueuer:
    """Thin wrapper around RQ so the route layer does not import rq."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._redis: Any = None
        self._queue: Any = None

    def _connect(self) -> None:
        if self._queue is not None:
            return
        import redis
        from rq import Queue

        self._redis = redis.Redis.from_url(self.settings.redis_url)
        self._queue = Queue(self.settings.queue_name, connection=self._redis)

    def enqueue(self, run_id: str) -> str:
        self._connect()
        job = self._queue.enqueue(
            "backend.worker.process_dispute_job",
            run_id,
            job_id=f"run:{run_id}",
            job_timeout=self.settings.job_timeout_s,
            result_ttl=600,
            failure_ttl=7 * 24 * 3600,
        )
        record_queue_depth(self.depth())
        return job.id

    def depth(self) -> int:
        try:
            self._connect()
            return int(self._queue.count)
        except Exception:  # noqa: BLE001 - a gauge is never worth failing a request
            return 0

    def ping(self) -> bool:
        try:
            self._connect()
            return bool(self._redis.ping())
        except Exception:  # noqa: BLE001
            return False


# --------------------------------------------------------------------------- #
# submit / read
# --------------------------------------------------------------------------- #
class RunService:
    def __init__(self, settings: Settings, store: RunStore, enqueuer: Any) -> None:
        self.settings = settings
        self.store = store
        self.enqueuer = enqueuer

    def submit(
        self,
        request: DisputeRequest,
        *,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[RunRecord, bool]:
        """Validate-adjacent create.  D1 validation already happened in the schema."""
        key = (idempotency_key or "").strip() or None
        if key is not None and not _IDEMPOTENCY_RE.match(key):
            raise ValueError("Idempotency-Key must be 1-200 printable ASCII characters")

        correlation = (correlation_id or "").strip()
        if not _CORRELATION_RE.match(correlation):
            correlation = f"corr_{uuid.uuid4().hex}"

        run_id = f"run_{uuid.uuid4().hex}"
        now = utcnow()
        record = RunRecord(
            run_id=run_id,
            correlation_id=correlation,
            status="queued",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            api_version=self.settings.api_version,
            service_version=self.settings.service_version,
        )
        try:
            record, created = self.store.create(record, idempotency_key=key)
        except Exception as exc:  # noqa: BLE001 - a store outage is a 503, not a 500
            logger.error("run store unavailable: %s: %s", type(exc).__name__, exc)
            raise StoreUnavailable("run store is unavailable; try again later") from exc
        if not created:
            return record, True

        try:
            job_id = self.enqueuer.enqueue(run_id)
        except Exception as exc:  # noqa: BLE001 - surface as 503, keep the reason for ops
            message = f"queue unavailable: {type(exc).__name__}: {exc}"[:2000]
            logger.error("enqueue failed run_id=%s: %s", run_id, message)
            try:
                self.store.fail(run_id, message, duration_s=0.0)
            except Exception:  # noqa: BLE001 - do not mask the enqueue failure
                logger.error("could not mark run failed run_id=%s", run_id, exc_info=True)
            raise QueueUnavailable("run queue is unavailable; try again later") from exc

        updated = self.store.update(run_id, queue_job_id=job_id)
        QUEUE_SUBMITTED.inc()
        return updated or record, False

    def get(self, run_id: str) -> RunRecord | None:
        return self.store.get(run_id)

    def recent(self, limit: int = 20) -> list[RunRecord]:
        return self.store.list_recent(limit)

    def ping(self) -> bool:
        return self.store.ping()


def summarise(record: RunRecord) -> RunSummary:
    result = record.result or {}
    status = result.get("status") if result else None
    if record.status == "failed":
        status = "failed"
    escalation = result.get("escalation") if result else None
    reason = escalation.get("reason_code") if escalation else None
    return RunSummary(
        run_id=record.run_id,
        status=record.status,  # type: ignore[arg-type]
        outcome=status if status in {"recommended", "escalated", "failed"} else None,
        reason_code=reason,
        route=result.get("route") if result else None,
        classification=result.get("classification") if result else None,
        created_at=record.created_at,
        completed_at=record.completed_at,
        duration_s=record.duration_s,
    )


# --------------------------------------------------------------------------- #
# execution (worker side)
# --------------------------------------------------------------------------- #
_retriever: Any = None
_retriever_lock = threading.Lock()


def get_retriever() -> Any:
    """One real retriever per worker process (loads the index and embedder once)."""
    global _retriever
    if _retriever is not None:
        return _retriever
    with _retriever_lock:
        if _retriever is None:
            from backend.retrieval.search import Retriever

            logger.info("loading retrieval index")
            _retriever = Retriever()
            logger.info("retrieval index loaded")
    return _retriever


def reset_retriever() -> None:
    global _retriever
    with _retriever_lock:
        _retriever = None


def default_runner(request: Any, run_id: str, correlation_id: str) -> Any:
    """The real graph, with the process-wide LLM client and retriever."""
    from backend.agents.graph import run_dispute_graph
    from backend.agents.llm import get_llm

    return run_dispute_graph(
        request, run_id, correlation_id, retriever=get_retriever(), llm=get_llm(),
    )


def process_run(
    run_id: str,
    *,
    settings: Settings | None = None,
    store: RunStore | None = None,
    runner: Callable[[Any, str, str], Any] | None = None,
) -> RunRecord:
    """Execute one queued run and persist the result, trace, metrics and archive.

    ``run_dispute_graph`` never raises, so an exception here is an infrastructure
    failure (index not available, worker misconfiguration).  The run is marked
    ``failed`` and the exception is re-raised so RQ records it.
    """
    settings = settings or Settings.from_env()
    store = store or build_store(settings)
    runner = runner or default_runner

    record = store.get(run_id)
    if record is None:
        raise ValueError(f"unknown run_id {run_id!r}")
    if record.status in {"completed", "failed"}:
        return record

    store.update(
        run_id,
        status="running",
        started_at=utcnow(),
        worker=f"{socket.gethostname()}:{threading.get_ident()}",
    )
    started = time.perf_counter()
    try:
        from backend.agents.state import DisputeInput

        agent_input = DisputeInput.model_validate(record.request)
        result = runner(agent_input, run_id, record.correlation_id)
        duration = time.perf_counter() - started
        result_json = result.model_dump(mode="json")
        trace_json = [entry.model_dump(mode="json") for entry in result.trace]
        updated = store.complete(run_id, result_json, trace_json, duration)
        try:
            record_run_metrics(result, duration)
        except Exception:  # noqa: BLE001 - metrics must never fail a completed run
            logger.warning("could not record metrics run_id=%s", run_id, exc_info=True)

        if updated is not None:
            try:
                archive_record(updated, settings)
            except Exception:  # noqa: BLE001 - the Redis copy remains authoritative
                ARCHIVE_FAILURES.inc()
                logger.warning("could not archive run_id=%s", run_id, exc_info=True)

        logger.info(
            "run finished run_id=%s status=%s duration_s=%.2f",
            run_id, result.status, duration,
        )
        return updated or record
    except Exception as exc:  # noqa: BLE001
        duration = time.perf_counter() - started
        message = f"{type(exc).__name__}: {exc}"[:2000]
        logger.exception("run failed run_id=%s", run_id)
        try:
            store.fail(run_id, message, duration_s=duration)
        except Exception:  # noqa: BLE001 - keep the original run failure
            logger.error("could not persist run failure run_id=%s", run_id, exc_info=True)
        try:
            record_run_failure(type(exc).__name__)
        except Exception:  # noqa: BLE001
            logger.warning("could not record failure metric run_id=%s", run_id, exc_info=True)
        raise
    finally:
        force_flush()


def reconstruction(record: RunRecord, settings: Settings) -> dict[str, Any]:
    """D5 view: request + result + trace + versions/config."""
    run_config: dict[str, Any] = {}
    for entry in record.trace:
        if entry.get("type") == "node" and entry.get("name") == "run.config":
            run_config = entry.get("detail") or {}
            break
    result = record.result or {}
    service = {
        "api_version": record.api_version,
        "service_version": record.service_version,
        "environment": settings.environment,
        "prompt_version": result.get("prompt_version"),
        "graph_config": run_config,
        "archive_dir": settings.run_archive_dir,
        "source": "redis",
    }
    return {
        "run_id": record.run_id,
        "correlation_id": record.correlation_id,
        "status": record.status,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "duration_s": record.duration_s,
        "request": record.request,
        "result": record.result,
        "trace": record.trace,
        "error": record.error,
        "worker": record.worker,
        "service": service,
    }
