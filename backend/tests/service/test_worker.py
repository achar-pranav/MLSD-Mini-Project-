from __future__ import annotations

import json
from pathlib import Path

import pytest
from prometheus_client import generate_latest

from backend.agents.state import Citation, DisputeResult, TraceEntry, Verification
from backend.app.schemas import DisputeRequest
from backend.app.services import process_run, reconstruction
from backend.app.storage import MemoryRunStore, RunRecord, load_archive, utcnow
from backend.app.telemetry import record_run_metrics


def _record(store: MemoryRunStore, payload: dict, run_id: str = "run_test") -> RunRecord:
    now = utcnow()
    record = RunRecord(
        run_id=run_id,
        correlation_id="corr_test",
        status="queued",
        request=DisputeRequest.model_validate(payload).model_dump(mode="json"),
        created_at=now,
        updated_at=now,
    )
    store.create(record)
    return record


def _result(run_id: str = "run_test") -> DisputeResult:
    return DisputeResult(
        run_id=run_id,
        status="recommended",
        classification="cnp_fraud",
        route="provisional_credit",
        basis="test basis",
        liability_position="zero",
        citations=[Citation(clause_id="6(ii)", doc_id="rbi_circular_2017", quote="quote")],
        verification=Verification(passed=True, attempts=1),
        prompt_version="analysis_v1.4+verification_v1.1",
        trace=[TraceEntry(type="llm", name="analysis", latency_ms=100, prompt_tokens=10,
                          completion_tokens=5, cost_usd=0.0001)],
    )


def test_process_run_persists_result_trace_and_archive(settings, payload):
    store = MemoryRunStore()
    record = _record(store, payload)
    calls = []

    def runner(request, run_id, correlation_id):
        calls.append((run_id, correlation_id))
        return _result(run_id)

    updated = process_run("run_test", settings=settings, store=store, runner=runner)
    assert calls == [("run_test", "corr_test")]
    assert updated.status == "completed"
    assert updated.result["status"] == "recommended"
    assert updated.trace[0]["name"] == "analysis"
    assert updated.duration_s is not None and updated.duration_s >= 0

    archive = (
        Path(settings.run_archive_dir)
        / record.created_at.date().isoformat()
        / "run_test.json"
    )
    assert archive.exists()
    payload_json = json.loads(archive.read_text(encoding="utf-8"))
    assert payload_json["run_id"] == "run_test"
    assert payload_json["result"]["route"] == "provisional_credit"
    assert payload_json["trace"][0]["type"] == "llm"

    # D5 fallback after the Redis TTL has expired
    archived = load_archive("run_test", settings)
    assert archived is not None
    assert archived["run_id"] == "run_test"
    assert load_archive("../etc/passwd", settings) is None


def test_process_run_failure_is_persisted(settings, payload):
    store = MemoryRunStore()
    _record(store, payload)

    def runner(*_args):
        raise RuntimeError("index is missing")

    with pytest.raises(RuntimeError, match="index is missing"):
        process_run("run_test", settings=settings, store=store, runner=runner)

    record = store.get("run_test")
    assert record.status == "failed"
    assert "RuntimeError: index is missing" in record.error
    assert record.completed_at is not None


def test_completed_run_is_not_reprocessed(settings, payload):
    store = MemoryRunStore()
    _record(store, payload)
    store.complete("run_test", _result().model_dump(mode="json"), [], 0.1)
    called = False

    def runner(*_args):
        nonlocal called
        called = True
        return _result()

    updated = process_run("run_test", settings=settings, store=store, runner=runner)
    assert called is False
    assert updated.status == "completed"


def test_metrics_include_run_and_step_observations():
    record_run_metrics(_result(), 2.5)
    text = generate_latest().decode("utf-8")
    assert "dispute_runs_total" in text
    assert 'outcome="recommended"' in text
    assert "dispute_run_duration_seconds" in text
    assert "dispute_step_latency_seconds" in text
    assert 'name="analysis"' in text
    assert "dispute_model_tokens_total" in text


def test_reconstruction_contains_versions_and_config(settings, payload):
    store = MemoryRunStore()
    _record(store, payload)
    process_run(
        "run_test", settings=settings, store=store,
        runner=lambda request, run_id, correlation_id: _result(run_id),
    )
    record = store.get("run_test")
    view = reconstruction(record, settings)
    assert view["service"]["prompt_version"] == "analysis_v1.4+verification_v1.1"
    assert view["service"]["api_version"] == "1"
    assert view["request"]["narrative"].startswith("A card-not-present")
