from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.storage import MemoryRunStore


class RecordingEnqueuer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.depth = 0

    def enqueue(self, run_id: str) -> str:
        self.calls.append(run_id)
        self.depth += 1
        return f"job:{run_id}"

    def ping(self) -> bool:
        return True


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    index = tmp_path / "index"
    index.mkdir()
    (index / "manifest.json").write_text("{}", encoding="utf-8")
    return Settings(
        run_store="memory",
        run_archive_dir=str(tmp_path / "runs"),
        index_dir=str(index),
        cors_origins=("*",),
        otel_endpoint="",
        otel_console=False,
        redis_url="redis://127.0.0.1:9/0",
    )


@pytest.fixture
def stack(settings: Settings):
    store = MemoryRunStore()
    enqueuer = RecordingEnqueuer()
    app = create_app(settings=settings, store=store, enqueuer=enqueuer)
    with TestClient(app) as client:
        yield client, store, enqueuer, settings


@pytest.fixture
def payload() -> dict:
    today = date.today()
    return {
        "narrative": "A card-not-present charge I never made appeared on my card. I reported it the next day.",
        "channel": "card_not_present",
        "amount": 4999,
        "transaction_date": (today - timedelta(days=3)).isoformat(),
        "communication_date": (today - timedelta(days=2)).isoformat(),
        "reported_date": today.isoformat(),
        "merchant": "Example Merchant",
        "account_type": "savings",
    }
