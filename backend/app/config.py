"""Deployment settings for the service layer.

Everything here is environment driven.  No provider keys, endpoints or model
identifiers belong in this file (requirement D4); the agent and retrieval
settings continue to live in ``.env`` / ``.env.example``.

Defaults are safe for local development.  In Docker Compose the values are
overridden explicitly; see ``docker-compose.yml`` and ``runbook.md``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be on/off, got {raw!r}")


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    """One immutable settings object per API or worker process."""

    service_name: str = "dispute-triage"
    service_version: str = "dev"
    environment: str = "dev"
    log_level: str = "INFO"
    api_version: str = "1"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: tuple[str, ...] = ("http://localhost:8501",)
    max_request_bytes: int = 64 * 1024

    # Queue and durable run state
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "dispute-runs"
    run_store: str = "redis"           # "redis" (deployments) or "memory" (tests)
    run_retention_s: int = 7 * 24 * 3600
    run_archive_dir: str = "data/runs"
    job_timeout_s: int = 240

    # Worker
    worker_metrics_addr: str = "0.0.0.0"
    worker_metrics_port: int = 9100
    worker_preload_retriever: bool = True

    # Retrieval (read by backend/retrieval/config.py as before)
    index_dir: str = "data/index"

    # OpenTelemetry
    otel_endpoint: str = ""            # e.g. http://jaeger:4318
    otel_console: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        store = _str("RUN_STORE", "redis").lower()
        if store not in {"redis", "memory"}:
            raise ValueError("RUN_STORE must be 'redis' or 'memory'")
        return cls(
            service_name=_str("OTEL_SERVICE_NAME", _str("SERVICE_NAME", "dispute-triage")),
            service_version=_str("SERVICE_VERSION", "dev"),
            environment=_str("ENVIRONMENT", "dev"),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            api_version=_str("API_VERSION", "1"),
            api_host=_str("API_HOST", "0.0.0.0"),
            api_port=_int("API_PORT", 8000),
            cors_origins=_csv("CORS_ORIGINS", "http://localhost:8501"),
            max_request_bytes=_int("MAX_REQUEST_BYTES", 64 * 1024),
            redis_url=_str("REDIS_URL", "redis://localhost:6379/0"),
            queue_name=_str("QUEUE_NAME", "dispute-runs"),
            run_store=store,
            run_retention_s=_int("RUN_RETENTION_S", 7 * 24 * 3600),
            run_archive_dir=_str("RUN_ARCHIVE_DIR", "data/runs"),
            job_timeout_s=_int("RUN_JOB_TIMEOUT_S", 240),
            worker_metrics_addr=_str("WORKER_METRICS_ADDR", "0.0.0.0"),
            worker_metrics_port=_int("WORKER_METRICS_PORT", 9100),
            worker_preload_retriever=_bool("WORKER_PRELOAD_RETRIEVER", True),
            index_dir=_str("INDEX_DIR", "data/index"),
            otel_endpoint=_str("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            otel_console=_bool("OTEL_CONSOLE", False),
        )

    @property
    def index_manifest(self) -> Path:
        return Path(self.index_dir) / "manifest.json"


def configure_logging(level: str) -> None:
    """One log format for the API and the worker; safe to call twice.

    If something already installed a handler (uvicorn, pytest, the container
    runtime) we leave its configuration alone.  Global level changes from
    library code leak into unrelated tests and third-party loggers.
    """
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
