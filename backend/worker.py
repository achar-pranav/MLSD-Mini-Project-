"""RQ worker entry point.

    python -m backend.worker

One process is one RQ worker.  ``LLM_MIN_INTERVAL_S`` is enforced per process,
so scaling to N workers multiplies the provider call rate by N.
"""
from __future__ import annotations

import logging
import os
import socket
from typing import Any

from backend.app.config import Settings, configure_logging
from backend.app.telemetry import record_queue_depth, setup_tracing, shutdown_tracing

logger = logging.getLogger("dispute.worker")

_settings: Settings | None = None
_store: Any = None


def _runtime() -> tuple[Settings, Any]:
    global _settings, _store
    if _settings is None:
        _settings = Settings.from_env()
    if _store is None:
        from backend.app.storage import build_store

        _store = build_store(_settings)
    return _settings, _store


def process_dispute_job(run_id: str) -> None:
    """RQ job entry point.  Must be importable by path (``backend.worker.*``)."""
    from backend.app.services import process_run

    settings, store = _runtime()
    process_run(run_id, settings=settings, store=store)


def main() -> None:
    from backend.agents.llm import load_dotenv_if_present

    load_dotenv_if_present()
    settings, _ = _runtime()

    configure_logging(settings.log_level)
    setup_tracing(settings)

    # Worker metrics live on their own port; Prometheus discovers scaled
    # workers with docker-compose DNS (see monitoring/prometheus.yml).
    from prometheus_client import start_http_server

    start_http_server(settings.worker_metrics_port, addr=settings.worker_metrics_addr)
    logger.info(
        "worker metrics on %s:%s", settings.worker_metrics_addr, settings.worker_metrics_port,
    )

    if settings.worker_preload_retriever:
        from backend.app.services import get_retriever

        logger.info("preloading retrieval index")
        get_retriever()

    import redis
    from rq import Queue, SimpleWorker

    connection = redis.Redis.from_url(settings.redis_url)
    queue = Queue(settings.queue_name, connection=connection)
    record_queue_depth(queue.count)
    # SimpleWorker executes the job in this process, so the Prometheus counters
    # recorded by process_run() are visible on :9100.  RQ's forking Worker would
    # record them in a child process and the metrics endpoint would stay empty.
    # Concurrency is therefore one run per worker container, as documented.
    worker = SimpleWorker(
        [queue],
        connection=connection,
        name=f"{socket.gethostname()}-{os.getpid()}",
    )
    logger.info(
        "worker listening queue=%s redis=%s concurrency=1 pacing_s=%s",
        settings.queue_name,
        settings.redis_url,
        os.environ.get("LLM_MIN_INTERVAL_S", "0"),
    )
    try:
        worker.work(with_scheduler=False)
    finally:
        shutdown_tracing()
        logger.info("worker stopped")


if __name__ == "__main__":
    main()
