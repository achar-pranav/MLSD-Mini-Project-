"""FastAPI application for the card-dispute triage service."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from backend.app.config import Settings
from backend.app.routes import router
from backend.app.services import RedisEnqueuer, RunService
from backend.app.storage import build_store
from backend.app.telemetry import (
    HTTP_DURATION,
    HTTP_REQUESTS,
    setup_tracing,
    shutdown_tracing,
)

logger = logging.getLogger("dispute.service.api")


def create_app(
    settings: Settings | None = None,
    *,
    store: Any = None,
    enqueuer: Any = None,
) -> FastAPI:
    """Factory used by uvicorn and by tests (tests inject a memory store)."""
    settings = settings or Settings.from_env()
    # Logging is owned by the process entry point (uvicorn / backend.worker),
    # not by the app factory: library code must not change the root logger.
    setup_tracing(settings)
    store = store or build_store(settings)
    enqueuer = enqueuer or RedisEnqueuer(settings)
    service = RunService(settings, store, enqueuer)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.info(
            "api starting service=%s version=%s store=%s queue=%s",
            settings.service_name, settings.service_version,
            settings.run_store, settings.queue_name,
        )
        try:
            yield
        finally:
            shutdown_tracing()

    app = FastAPI(
        title="Card Dispute Triage",
        version=settings.service_version,
        description=(
            "Asynchronous wrapper around the two-agent dispute graph. "
            "POST /disputes, then poll GET /runs/{run_id}."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.enqueuer = enqueuer
    app.state.service = service

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Idempotency-Key", "X-Correlation-ID"],
            expose_headers=["Location"],
        )

    @app.middleware("http")
    async def enforce_request_size(request: Request, call_next):
        raw = request.headers.get("content-length")
        if raw:
            try:
                length = int(raw)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
            if length > settings.max_request_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"request body exceeds {settings.max_request_bytes} bytes"},
                )
        return await call_next(request)

    @app.middleware("http")
    async def observe_http(request: Request, call_next):
        if request.url.path in {"/metrics", "/health", "/ready"}:
            return await call_next(request)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = request.scope.get("route")
            route_path = getattr(route, "path", None) or "unmatched"
            HTTP_REQUESTS.labels(request.method, route_path, str(status_code)).inc()
            HTTP_DURATION.labels(request.method, route_path).observe(elapsed)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(router)
    return app


app = create_app()
