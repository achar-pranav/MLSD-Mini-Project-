from __future__ import annotations

from fastapi.testclient import TestClient

from backend.agents.state import Citation, DisputeResult, TraceEntry, Verification
from backend.app.main import create_app
from backend.app.services import process_run
from backend.app.storage import MemoryRunStore, RunRecord, utcnow


class FailingEnqueuer:
    def enqueue(self, run_id: str) -> str:
        raise RuntimeError("redis connection refused")

    def ping(self) -> bool:
        return False


def _complete(store, run_id: str):
    result = DisputeResult(
        run_id=run_id,
        status="recommended",
        classification="cnp_fraud",
        reporting_delay_working_days=2,
        route="provisional_credit",
        basis="Para 6(ii): third-party breach notified within three working days.",
        liability_position="Zero liability for the customer.",
        citations=[Citation(
            clause_id="6(ii)",
            doc_id="rbi_circular_2017",
            quote="A customer's liability shall be zero if the unauthorised transaction is reported within three working days.",
        )],
        verification=Verification(passed=True, attempts=1),
        prompt_version="analysis_v1.4+verification_v1.1",
    )
    trace = [
        TraceEntry(type="llm", name="analysis", latency_ms=1000, prompt_tokens=100,
                   completion_tokens=50, cost_usd=0.001),
        TraceEntry(type="tool", name="search_corpus", latency_ms=25),
        TraceEntry(type="node", name="run.config", detail={"seed_provisions": True}),
    ]
    store.complete(run_id, result.model_dump(mode="json"),
                   [entry.model_dump(mode="json") for entry in trace], 1.2)


def test_health_and_ready(stack):
    client, _, _, _ = stack
    assert client.get("/health").json()["status"] == "ok"
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "store": True, "index": True, "queue": True}


def test_submit_returns_202_and_enqueues(stack, payload):
    client, _, enqueuer, _ = stack
    response = client.post("/disputes", json=payload)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["run_id"].startswith("run_")
    assert body["idempotent_replay"] is False
    assert enqueuer.calls == [body["run_id"]]
    assert response.headers["location"] == f"/runs/{body['run_id']}"


def test_d1_validation_rejects_before_enqueue(stack, payload):
    client, _, enqueuer, _ = stack
    bad = dict(payload, amount=0)
    response = client.post("/disputes", json=bad)
    assert response.status_code == 422
    assert enqueuer.calls == []


def test_oversized_body_is_413(stack, payload):
    client, _, enqueuer, _ = stack
    response = client.post("/disputes", json=dict(payload, narrative="x" * 70_000))
    assert response.status_code == 413
    assert enqueuer.calls == []


def test_idempotency_replay_does_not_enqueue_twice(stack, payload):
    client, _, enqueuer, _ = stack
    first = client.post("/disputes", json=payload, headers={"Idempotency-Key": "case-1"})
    second = client.post("/disputes", json=payload, headers={"Idempotency-Key": "case-1"})
    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    assert second.json()["idempotent_replay"] is True
    assert len(enqueuer.calls) == 1


def test_invalid_idempotency_key_is_400(stack, payload):
    client, _, _, _ = stack
    response = client.post("/disputes", json=payload, headers={"Idempotency-Key": "k" * 300})
    assert response.status_code == 400


def test_get_run_trace_and_reconstruction(stack, payload):
    client, store, _, _ = stack
    run_id = client.post("/disputes", json=payload).json()["run_id"]
    _complete(store, run_id)

    view = client.get(f"/runs/{run_id}").json()
    assert view["status"] == "completed"
    assert view["result"]["status"] == "recommended"
    assert view["result"]["route"] == "provisional_credit"
    assert len(view["trace"]) == 3
    assert view["duration_s"] == 1.2

    small = client.get(f"/runs/{run_id}", params={"include_trace": "false"}).json()
    assert small["trace"] is None
    assert small["result"]["trace"] == []

    trace = client.get(f"/runs/{run_id}/trace").json()
    assert [entry["name"] for entry in trace] == ["analysis", "search_corpus", "run.config"]

    recon = client.get(f"/runs/{run_id}/reconstruction").json()
    assert recon["request"]["amount"] == 4999
    assert recon["service"]["prompt_version"] == "analysis_v1.4+verification_v1.1"
    assert recon["service"]["graph_config"] == {"seed_provisions": True}


def test_recent_runs(stack, payload):
    client, store, _, _ = stack
    run_id = client.post("/disputes", json=payload).json()["run_id"]
    _complete(store, run_id)
    body = client.get("/runs", params={"limit": 10}).json()
    assert body["count"] == 1
    assert body["runs"][0]["run_id"] == run_id
    assert body["runs"][0]["outcome"] == "recommended"


def test_unknown_run_is_404(stack):
    client, _, _, _ = stack
    assert client.get("/runs/run_missing").status_code == 404
    assert client.get("/runs/run_missing/trace").status_code == 404
    assert client.get("/runs/run_missing/reconstruction").status_code == 404


def test_metrics_exposes_service_metrics(stack, payload):
    client, _, _, _ = stack
    client.post("/disputes", json=payload)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "dispute_runs_total" in response.text
    assert "http_requests_total" in response.text


def test_reconstruction_falls_back_to_archive_after_redis_ttl(settings, payload):
    store = MemoryRunStore()
    now = utcnow()
    store.create(RunRecord(
        run_id="run_archived",
        correlation_id="corr_archived",
        status="queued",
        request=payload,
        created_at=now,
        updated_at=now,
    ))
    process_run(
        "run_archived", settings=settings, store=store,
        runner=lambda request, run_id, correlation_id: DisputeResult(
            run_id=run_id,
            status="escalated",
            escalation={"reason_code": "out_of_corpus", "note": "not covered"},
            verification=Verification(passed=False, attempts=1),
            prompt_version="analysis_v1.4+verification_v1.1",
        ),
    )
    empty_store = MemoryRunStore()
    app = create_app(settings=settings, store=empty_store, enqueuer=FailingEnqueuer())
    with TestClient(app) as client:
        response = client.get("/runs/run_archived/reconstruction")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["service"]["source"] == "archive"
    assert body["result"]["escalation"]["reason_code"] == "out_of_corpus"


def test_queue_outage_is_503_and_run_is_failed(settings, payload):
    store = MemoryRunStore()
    app = create_app(settings=settings, store=store, enqueuer=FailingEnqueuer())
    with TestClient(app) as client:
        response = client.post("/disputes", json=payload)
        assert response.status_code == 503
    records = store.list_recent()
    assert len(records) == 1
    assert records[0].status == "failed"
    assert "queue unavailable" in records[0].error
