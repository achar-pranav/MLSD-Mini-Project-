# Runbook — Card Dispute Triage service layer

This is the operational guide for Member 3's slice: API, worker, run store,
metrics/tracing, frontend, load test and Docker.  The agent layer's behaviour is
documented in `backend/agents/README.md`; retrieval in
`backend/retrieval/README.md`.

## 1. Architecture

```
client / Streamlit
      │ POST /disputes (D1 validation)
      ▼
   FastAPI (api) ──enqueue──▶ Redis queue (rq) ──▶ worker (N processes)
      │                                             │
      │                                             ├─ run_dispute_graph()
      │                                             ├─ Prometheus metrics :9100
      │                                             ├─ OTLP spans ──▶ Jaeger
      │                                             └─ result + trace ──▶ Redis
      │                                                                    │
      └──── GET /runs/{id}, /trace, /reconstruction ◀──────────────────────┘
                                   │
                                   └── JSON archive volume (/data/runs)

Prometheus scrapes api:8000/metrics and every worker:9100/metrics.
Grafana provisions the "Dispute Triage Service" dashboard.
```

- `POST /disputes` returns `202` as soon as the run is queued. D1 validation
  happens before that point, so a `422` creates no run and costs no model call.
- The worker calls `run_dispute_graph(request, run_id, correlation_id)`. The
  graph never raises for agent failures; it returns an escalation. An exception
  in the worker is an infrastructure failure and is stored as `status=failed`.
- Results and traces are kept in Redis for `RUN_RETENTION_S` (default 7 days)
  and archived as JSON for D5 reconstruction.

## 2. Environment

Copy `.env.example` to `.env` (untracked) and set:

- `LLM_*` — provider base URL, model id, key. No real values are committed.
- `EMBEDDING_MODEL` — copy `embedder.model` from `data/index/manifest.json`.
- `REDIS_URL`, `RUN_*`, `OTEL_*` — service settings; defaults are in
  `.env.example` and `backend/app/config.py`.

Never commit `.env`. Check before committing:

```bash
git grep -nEi "gemini[-]|google[a]pis|AI[z]a|AQ[.]" -- . ':!*.lock'
git status --short
```

## 3. Local development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r backend/requirements.txt -r requirements-dev.txt

# Redis is the only external dependency
docker run --rm -d -p 6379:6379 --name dispute-redis redis:7.4.2-alpine

# terminal 1 (uvicorn reads the untracked .env itself)
.venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload --env-file .env
# terminal 2 (the worker calls load_dotenv_if_present() itself)
.venv/bin/python -m backend.worker
# terminal 3
API_BASE_URL=http://localhost:8000 .venv/bin/streamlit run frontend/app.py
```

If you do not have the embedding model cached locally, set
`HF_HUB_OFFLINE=0` for the first worker start, then back to `1`.

## 4. Docker

### 4.1 Build with the embedding model cached in the image (recommended)

```bash
cp .env.example .env
# edit .env: EMBEDDING_MODEL, LLM_*, optionally HF_HUB_OFFLINE=1
docker compose up -d --build
```

Compose passes `EMBEDDING_MODEL` as a build arg; the Dockerfile downloads it
into `HF_HOME=/models/hf`. The runtime sets `HF_HUB_OFFLINE=1`, so a missing
cache fails at worker startup instead of silently calling Hugging Face.

### 4.2 Alternative: named cache volume

```bash
# first start downloads the model; HF_HUB_OFFLINE defaults to 0 in this overlay
docker compose -f docker-compose.yml -f docker-compose.cache.yml up -d --build
# after the worker has loaded once, set HF_HUB_OFFLINE=1 in .env and restart
docker compose -f docker-compose.yml -f docker-compose.cache.yml up -d
```

Do not use the cache overlay with a baked image. The empty volume hides the
baked model.

### 4.3 URLs

| Service | URL |
|---|---|
| API + OpenAPI | http://localhost:8000/docs |
| API metrics | http://localhost:8000/metrics |
| Frontend | http://localhost:8501 |
| Jaeger UI | http://localhost:16686 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (`admin` / password from `.env`) |

### 4.4 Python/FAISS pin

The image pins **Python 3.12** because FAISS and sentence-transformers publish
wheels for it. Python 3.14 (the agent author's local version) does not have a
reliable FAISS wheel; do not change the base image without a container smoke
test:

```bash
docker compose run --rm worker python -c "import faiss, sentence_transformers; print('ok')"
```

## 5. API

```bash
# submit
curl -s -X POST localhost:8000/disputes \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' \
  -d '{
    "narrative": "A card-not-present charge I never made appeared on my card. I reported it the next day.",
    "channel": "card_not_present",
    "amount": 4999,
    "transaction_date": "2026-11-05",
    "communication_date": "2026-11-06",
    "reported_date": "2026-11-07",
    "account_type": "savings"
  }'

# poll
curl -s localhost:8000/runs/<run_id>

# trace and D5 reconstruction
curl -s localhost:8000/runs/<run_id>/trace
curl -s localhost:8000/runs/<run_id>/reconstruction
```

`Idempotency-Key` is optional; a replay returns the original `run_id` with
`idempotent_replay=true` and does not enqueue a second job. The header is kept
for 7 days (`RUN_RETENTION_S`).

`GET /runs?limit=20` powers the UI's recent-runs list. `include_trace=false`
returns a smaller result body.

`GET /requests?limit=50` returns the queries the analyst submitted, newest first,
from the append-only `RUN_HISTORY_FILE` (`data/requests.jsonl` by default). The UI
uses it as "Saved queries" so a case can be reloaded into the form instead of
being retyped. Replayed idempotent submissions are not logged twice.

## 6. Scaling and pacing

The worker uses RQ's `SimpleWorker`, so one run executes per worker process and
the Prometheus counters recorded by the job are visible on that worker's
`:9100/metrics` endpoint. A forking worker would hide them in a child process.
Scale by adding worker containers, not by increasing per-container concurrency.
`SimpleWorker` does not hard-kill a stuck job; the graph's `AGENT_WALL_CLOCK_S`,
tool timeout and per-model-call timeout are the guardrails.

`LLM_MIN_INTERVAL_S` is enforced **per worker process**. With `--scale 3`, the
provider sees about three times the call rate. Before scaling:

```bash
# free tier: 60 / RPM / workers
USER_RPM=15
WORKERS=3
python - <<'PY'
print(60 / 15 / 3)
PY
```

Set `LLM_MIN_INTERVAL_S` accordingly, or use a paid tier. `AGENT_WALL_CLOCK_S`
must be greater than the expected queue of paced model calls; the default 60 s
is for the default pacing.

```bash
docker compose up -d --scale worker=3 --no-recreate
```

Prometheus discovers each worker through Docker DNS (`dns_sd_configs` in
`monitoring/prometheus.yml`). Do not publish 9100 on the host; it would collide
between replicas.

## 7. Observability

### Metrics (Prometheus)

Run metrics are exposed by each worker on `:9100/metrics`; the API exposes
HTTP/queue metrics on `:8000/metrics`. Prometheus scrapes both jobs, so the
dashboard aggregates them. The API cannot show worker counters because it never
executes the graph.

- `dispute_runs_total{outcome,reason_code,classification}` — success rate,
  escalation reasons.
- `dispute_run_duration_seconds` — end-to-end worker duration.
- `dispute_run_steps`, `dispute_run_model_calls`, `dispute_run_tool_calls`.
- `dispute_step_latency_seconds{type,name}` — from `result.trace`.
- `dispute_run_retries`, `dispute_retries_total` — verifier retries.
- `dispute_model_tokens_total{direction}`, `dispute_model_cost_usd_total`.
- `dispute_queue_depth`, `dispute_queue_submitted_total`.
- `http_requests_total`, `http_request_duration_seconds`.

Alert rules: `monitoring/alerts.yml`. SLO definitions:
`monitoring/SLO.md` (declared before measurement).

### Traces (OpenTelemetry → Jaeger)

`OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318` is set in compose. The API and
worker install a `TracerProvider` with a batch OTLP HTTP exporter. Agent node,
tool and model spans already exist; the service adds no new instrumentation
inside the graph. Search Jaeger for `service.name=dispute-triage`,
`run_id`, or `correlation_id`.

If the endpoint is empty, spans are created but dropped. `OTEL_CONSOLE=1` prints
them instead, for debugging.

### Logs

Both processes log one line per event. Agent events are JSON objects inside the
message and always carry `run_id` and `correlation_id`. Use `docker compose logs
-f worker` and filter by run id.

## 8. D5 run reconstruction

Four layers:

1. `result.trace` — the per-step list returned with the result.
2. Redis hash `dispute:run:<run_id>` — status, request, result, trace, timings,
   worker, duration. TTL `RUN_RETENTION_S`.
3. JSON archive — `$RUN_ARCHIVE_DIR/YYYY-MM-DD/<run_id>.json`, written
   atomically by the worker. Contains the request, result, trace, versions and
   `archived_at`. Never expires.
4. Query history — `$RUN_HISTORY_FILE` (`data/requests.jsonl`), appended by the
   API at submit time. It keeps what the analyst typed, so a case can be
   replayed even when both Redis and the archive are empty. Never expires.

`GET /runs`, `GET /runs/{run_id}` and `GET /runs/{run_id}/trace` read Redis and
fall back to the archive, so run history stays visible after a Redis restart or
after the TTL. `GET /runs/{run_id}/reconstruction` adds service metadata (prompt
version, graph budget/seed config) and reports `service.source` as `redis` or
`archive`. `GET /requests` serves the query history.

## 9. Failure handling

| Symptom | Meaning | Action |
|---|---|---|
| `status=failed`, error `queue unavailable` | API could not enqueue | Check Redis; retry with a new idempotency key |
| Worker exits at startup, `IndexError`/`FileNotFound` | `data/index` missing or model cache absent | Check the bind mount and `HF_HUB_OFFLINE`/build arg |
| All runs escalate `provider_unavailable` | provider down or breaker open | Check `LLM_BASE_URL`, key, quota; fallback provider |
| `verification_failed` rate high | prompt/model changed or corpus mismatch | Inspect `GET /runs/{id}/trace`, compare prompt version |
| p95 above SLO | provider latency or queue backlog | Check `dispute_queue_depth`, add workers, adjust pacing |
| `422` on submit | D1 validation | Read the field path in `detail`; no run was created |
| Worker metrics scrape fails after scaling | DNS/port discovery | Confirm `dns_sd_configs` and that 9100 is exposed, not published |

The worker marks an infrastructure failure `failed` and stores a sanitized
error string. `run_dispute_graph` itself always returns a `DisputeResult`; a
failure here means the worker could not invoke it (index, imports, Redis).

## 10. Tests

```bash
.venv/bin/python -m pytest backend/tests/agents -q   # Member 2
.venv/bin/python -m pytest backend/tests/service -q  # Member 3
```

Service tests use a memory store and a recording enqueuer; no Redis or real
model call is required.

## 11. Security

- The API is unauthenticated in this deployment. Put it behind the bank's
  ingress/SSO; do not expose it directly to the internet.
- `narrative` and `merchant` are untrusted free text. Boundary validation checks
  shape, not intent; injection is handled in the graph before any model call.
- Do not log request bodies in production. The service logs run ids and
  outcomes, not narratives.
- Run containers as UID 10001 (both images do).
- Rotate `GRAFANA_ADMIN_PASSWORD`; the compose default is for local use only.
- Keep `.env` untracked and never put real provider identifiers in tracked
  files (D4).

## 12. Rollback

The API and worker are stateless except for Redis and the archive volume:

```bash
docker compose down                 # keeps volumes
docker compose up -d --build        # previous image tag, if using one
```

Queue compatibility: jobs are referenced by the import path
`backend.worker.process_dispute_job`. If you rename that function, drain the
queue first (`docker compose stop worker`, wait for `dispute_queue_depth=0`,
then rename), or old jobs fail as unknown jobs.
