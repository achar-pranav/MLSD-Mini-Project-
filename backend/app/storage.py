"""Durable run state (D5) and the JSON archive.

The queue is Redis; run state is deliberately kept in the same dependency so a
deployment needs one stateful service, not three.  Every field is stored as a
JSON string in a Redis hash keyed by ``run_id``, which makes the representation
identical to the archive file and easy to inspect with ``HGETALL``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from backend.app.config import Settings

logger = logging.getLogger("dispute.service.storage")

FIELDS = (
    "run_id", "correlation_id", "status", "request", "result", "trace", "error",
    "created_at", "updated_at", "started_at", "completed_at", "duration_s",
    "worker", "queue_job_id", "api_version", "service_version",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False)


@dataclass
class RunRecord:
    run_id: str
    correlation_id: str
    status: str
    request: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_s: float | None = None
    worker: str | None = None
    queue_job_id: str | None = None
    api_version: str = "1"
    service_version: str = "dev"

    def to_fields(self) -> dict[str, str]:
        return {name: _dumps(getattr(self, name)) for name in FIELDS}

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> "RunRecord":
        decoded: dict[str, Any] = {}
        for name in FIELDS:
            raw = fields.get(name)
            if raw is None:
                continue
            try:
                decoded[name] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                decoded[name] = raw
        for name in ("created_at", "updated_at", "started_at", "completed_at"):
            value = decoded.get(name)
            if isinstance(value, str):
                try:
                    decoded[name] = datetime.fromisoformat(value)
                except ValueError:
                    decoded[name] = None
        if decoded.get("trace") is None:
            decoded["trace"] = []
        return cls(**decoded)

    def to_archive(self) -> dict[str, Any]:
        payload = {name: getattr(self, name) for name in FIELDS}
        payload["archived_at"] = utcnow().isoformat()
        return payload


class RunStore(Protocol):
    def create(self, record: RunRecord, idempotency_key: str | None = None) -> tuple[RunRecord, bool]: ...
    def get(self, run_id: str) -> RunRecord | None: ...
    def update(self, run_id: str, **fields: Any) -> RunRecord | None: ...
    def complete(self, run_id: str, result: dict, trace: list[dict], duration_s: float) -> RunRecord | None: ...
    def fail(self, run_id: str, error: str, duration_s: float | None = None) -> RunRecord | None: ...
    def list_recent(self, limit: int = 20) -> list[RunRecord]: ...
    def ping(self) -> bool: ...


class MemoryRunStore:
    """Process-local store for tests and single-process demos.  Not for deploy."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = threading.RLock()

    def create(self, record: RunRecord, idempotency_key: str | None = None) -> tuple[RunRecord, bool]:
        with self._lock:
            if idempotency_key:
                existing_id = self._idempotency.get(idempotency_key)
                if existing_id and existing_id in self._runs:
                    return copy.deepcopy(self._runs[existing_id]), False
                self._idempotency[idempotency_key] = record.run_id
            self._runs[record.run_id] = copy.deepcopy(record)
            return copy.deepcopy(record), True

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            record = self._runs.get(run_id)
            return copy.deepcopy(record) if record else None

    def update(self, run_id: str, **fields: Any) -> RunRecord | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            for key, value in fields.items():
                if key in FIELDS:
                    setattr(record, key, value)
            record.updated_at = utcnow()
            return copy.deepcopy(record)

    def complete(self, run_id: str, result: dict, trace: list[dict], duration_s: float) -> RunRecord | None:
        return self.update(
            run_id, status="completed", result=result, trace=trace,
            completed_at=utcnow(), duration_s=duration_s, error=None,
        )

    def fail(self, run_id: str, error: str, duration_s: float | None = None) -> RunRecord | None:
        return self.update(
            run_id, status="failed", error=error, completed_at=utcnow(), duration_s=duration_s,
        )

    def list_recent(self, limit: int = 20) -> list[RunRecord]:
        with self._lock:
            records = sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)
            return [copy.deepcopy(r) for r in records[:limit]]

    def ping(self) -> bool:
        return True


class RedisRunStore:
    """Redis hash per run plus a sorted-set index for the recent-runs view."""

    def __init__(self, url: str, retention_s: int = 7 * 24 * 3600, prefix: str = "dispute:") -> None:
        import redis

        self._redis = redis.Redis.from_url(url, decode_responses=True)
        self._retention_s = max(60, int(retention_s))
        self._prefix = prefix

    def _key(self, run_id: str) -> str:
        return f"{self._prefix}run:{run_id}"

    def _index(self) -> str:
        return f"{self._prefix}runs"

    def _idem_key(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"{self._prefix}idem:{digest}"

    def create(self, record: RunRecord, idempotency_key: str | None = None) -> tuple[RunRecord, bool]:
        if idempotency_key:
            idem_key = self._idem_key(idempotency_key)
            if not self._redis.set(idem_key, record.run_id, nx=True, ex=self._retention_s):
                existing_id = self._redis.get(idem_key)
                existing = self.get(existing_id) if existing_id else None
                if existing is not None:
                    return existing, False
                # Stale pointer: the run expired. Remove and try once more.
                self._redis.delete(idem_key)
                self._redis.set(idem_key, record.run_id, nx=True, ex=self._retention_s)

        pipe = self._redis.pipeline()
        pipe.hset(self._key(record.run_id), mapping=record.to_fields())
        pipe.expire(self._key(record.run_id), self._retention_s)
        pipe.zadd(self._index(), {record.run_id: record.created_at.timestamp()})
        pipe.expire(self._index(), self._retention_s)
        pipe.execute()
        return copy.deepcopy(record), True

    def get(self, run_id: str) -> RunRecord | None:
        if not run_id:
            return None
        fields = self._redis.hgetall(self._key(run_id))
        return RunRecord.from_fields(fields) if fields else None

    def update(self, run_id: str, **fields: Any) -> RunRecord | None:
        if not self._redis.exists(self._key(run_id)):
            return None
        allowed = {key: value for key, value in fields.items() if key in FIELDS}
        allowed["updated_at"] = utcnow()
        mapping = {key: _dumps(value) for key, value in allowed.items()}
        pipe = self._redis.pipeline()
        pipe.hset(self._key(run_id), mapping=mapping)
        pipe.expire(self._key(run_id), self._retention_s)
        pipe.execute()
        return self.get(run_id)

    def complete(self, run_id: str, result: dict, trace: list[dict], duration_s: float) -> RunRecord | None:
        return self.update(
            run_id, status="completed", result=result, trace=trace,
            completed_at=utcnow(), duration_s=duration_s, error=None,
        )

    def fail(self, run_id: str, error: str, duration_s: float | None = None) -> RunRecord | None:
        return self.update(
            run_id, status="failed", error=error, completed_at=utcnow(), duration_s=duration_s,
        )

    def list_recent(self, limit: int = 20) -> list[RunRecord]:
        cutoff = utcnow().timestamp() - self._retention_s
        try:
            self._redis.zremrangebyscore(self._index(), 0, cutoff)
            ids = self._redis.zrevrange(self._index(), 0, max(0, limit * 2 - 1))
        except Exception:  # noqa: BLE001 - a store hiccup must not break the API
            logger.warning("recent-runs index unavailable", exc_info=True)
            return []
        records: list[RunRecord] = []
        for run_id in ids:
            record = self.get(run_id)
            if record is None:
                self._redis.zrem(self._index(), run_id)
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:  # noqa: BLE001
            return False


def build_store(settings: Settings) -> RunStore:
    if settings.run_store == "memory":
        return MemoryRunStore()
    return RedisRunStore(settings.redis_url, retention_s=settings.run_retention_s)


_ARCHIVE_RUN_ID = re.compile(r"^run_[A-Za-z0-9]+$")


def load_archive(run_id: str, settings: Settings) -> dict[str, Any] | None:
    """Read the JSON archive after the Redis TTL has expired (D5 fallback)."""
    if not _ARCHIVE_RUN_ID.match(run_id):
        return None
    root = Path(settings.run_archive_dir) if settings.run_archive_dir else None
    if root is None or not root.exists():
        return None
    try:
        directories = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    except OSError:
        logger.warning("could not list archive root %s", root, exc_info=True)
        return None
    for directory in directories:
        path = directory / f"{run_id}.json"
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("unreadable archive %s", path, exc_info=True)
                return None
    return None


def archive_record(record: RunRecord, settings: Settings) -> Path:
    """Write the complete run (request, result, trace) as one JSON file."""
    root = Path(settings.run_archive_dir) if settings.run_archive_dir else None
    if root is None:
        raise ValueError("RUN_ARCHIVE_DIR is empty")
    day = record.created_at.date().isoformat()
    directory = root / day
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{record.run_id}.json"
    payload = record.to_archive()
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".tmp-", delete=False,
    ) as handle:
        json.dump(payload, handle, default=_json_default, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, destination)
    return destination
