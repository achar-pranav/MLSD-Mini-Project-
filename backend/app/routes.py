"""HTTP routes for the dispute service."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status

from backend.agents.state import DisputeResult, TraceEntry
from backend.app.schemas import (
    DisputeRequest,
    Health,
    Readiness,
    RequestHistory,
    RequestHistoryEntry,
    RunList,
    RunReconstruction,
    RunSubmission,
    RunSummary,
    RunView,
)
from backend.app.services import (
    QueueUnavailable,
    RunService,
    StoreUnavailable,
    reconstruction,
    summarise,
)
from backend.app.storage import (
    list_archives,
    load_archive,
    read_request_history,
    record_from_archive,
)

router = APIRouter()


def _service(request: Request) -> RunService:
    return request.app.state.service


def _record(service: RunService, run_id: str) -> Any:
    """Redis first, then the JSON archive (history survives Redis restarts)."""
    record = service.get(run_id)
    if record is not None:
        return record
    payload = load_archive(run_id, service.settings)
    return record_from_archive(payload) if payload else None


def _view(record: Any, include_trace: bool) -> RunView:
    result = DisputeResult.model_validate(record.result) if record.result else None
    trace = [TraceEntry.model_validate(item) for item in record.trace]
    if result is not None and not include_trace:
        result = result.model_copy(update={"trace": []})
    return RunView(
        run_id=record.run_id,
        correlation_id=record.correlation_id,
        status=record.status,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        duration_s=record.duration_s,
        request=DisputeRequest.model_validate(record.request),
        result=result,
        trace=trace if include_trace else None,
        error=record.error,
        worker=record.worker,
    )


@router.post(
    "/disputes",
    response_model=RunSubmission,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a dispute for asynchronous triage",
)
def submit_dispute(
    payload: DisputeRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
) -> RunSubmission:
    """D1 validation happens before this function runs (FastAPI + Pydantic).

    A 422 means no run was created and no model call will be charged.
    """
    service = _service(request)
    try:
        record, replay = service.submit(
            payload, idempotency_key=idempotency_key, correlation_id=correlation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (QueueUnavailable, StoreUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    response.headers["Location"] = f"/runs/{record.run_id}"
    return RunSubmission(
        run_id=record.run_id,
        correlation_id=record.correlation_id,
        status=record.status,  # type: ignore[arg-type]
        created_at=record.created_at,
        idempotent_replay=replay,
    )


@router.get(
    "/requests",
    response_model=RequestHistory,
    summary="Submitted queries, newest first (append-only history file)",
)
def list_requests(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
) -> RequestHistory:
    """What the analyst typed, kept even after the run leaves Redis."""
    entries: list[RequestHistoryEntry] = []
    for entry in read_request_history(_service(request).settings, limit):
        try:
            entries.append(RequestHistoryEntry.model_validate(entry))
        except ValueError:
            continue
    return RequestHistory(requests=entries, count=len(entries))


@router.get("/runs", response_model=RunList, summary="Recent runs")
def list_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> RunList:
    service = _service(request)
    records = service.recent(limit)
    if len(records) < limit:
        seen = {record.run_id for record in records}
        for archived in list_archives(service.settings, limit):
            if archived.run_id not in seen:
                records.append(archived)
                seen.add(archived.run_id)
            if len(records) >= limit:
                break
    records.sort(key=lambda record: record.created_at, reverse=True)
    runs: list[RunSummary] = [summarise(record) for record in records[:limit]]
    return RunList(runs=runs, count=len(runs))


@router.get("/runs/{run_id}", response_model=RunView, summary="One run and its result")
def get_run(
    run_id: str,
    request: Request,
    include_trace: bool = Query(default=True, description="Set false for a smaller response."),
) -> RunView:
    record = _record(_service(request), run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _view(record, include_trace)


@router.get(
    "/runs/{run_id}/trace",
    response_model=list[TraceEntry],
    summary="The per-step trace for D5 run reconstruction",
)
def get_trace(run_id: str, request: Request) -> list[TraceEntry]:
    record = _record(_service(request), run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [TraceEntry.model_validate(item) for item in record.trace]


@router.get(
    "/runs/{run_id}/reconstruction",
    response_model=RunReconstruction,
    summary="Everything needed to explain a past run after the fact",
)
def get_reconstruction(run_id: str, request: Request) -> RunReconstruction:
    service = _service(request)
    record = _record(service, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    view = reconstruction(record, service.settings)
    if service.get(run_id) is None:
        # Redis no longer has it (TTL expired or Redis restarted): say so.
        view["service"]["source"] = "archive"
    return RunReconstruction.model_validate(view)


@router.get("/health", response_model=Health, summary="Liveness")
def health(request: Request) -> Health:
    settings = request.app.state.settings
    return Health(status="ok", service=settings.service_name, version=settings.service_version)


@router.get("/ready", response_model=Readiness, summary="Readiness (store, index, queue)")
def ready(request: Request) -> Readiness:
    settings = request.app.state.settings
    service: RunService = request.app.state.service
    store_ok = service.ping()
    index_ok = settings.index_manifest.exists()
    enqueuer = service.enqueuer
    queue_ok = bool(enqueuer.ping()) if hasattr(enqueuer, "ping") else True
    if not (store_ok and index_ok and queue_ok):
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "store": store_ok,
                "index": index_ok,
                "queue": queue_ok,
            },
        )
    return Readiness(status="ready", store=True, index=True, queue=True)
