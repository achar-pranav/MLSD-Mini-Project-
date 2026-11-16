# Running the demo locally (laptop only)

Docker is **not** required. The demo is four ordinary processes:

```
redis-server  ->  API (uvicorn)  ->  worker (python)  ->  Streamlit console
                       |                  |
                       +---- Redis queue -+
```

Everything stays on your machine at `localhost`.

---

## 1. What you need once

- Python **3.12** (FAISS needs it)
- Redis installed locally (`redis-server`)
- The repo at `~/code/MLSD-Mini-Project-`
- `.env` in the repo root (already there; untracked)

### If you ever need to recreate the virtualenv

```bash
cd ~/code/MLSD-Mini-Project-

python3.12 -m venv .venv
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv/bin/pip install -r requirements.txt \
                    -r backend/requirements.txt \
                    -r requirements-dev.txt \
                    -r frontend/requirements.txt
```

### If you ever need to recreate `.env`

```bash
cp .env.example .env
```

Set at least:

| Variable | Value |
|---|---|
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` (from `data/index/manifest.json`) |
| `LLM_PROVIDER` | `openai` for the real provider, `fake` for a keyless demo |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` (when using a real key) |
| `LLM_MODEL` | `deepseek-chat` |
| `LLM_API_KEY` | your key |
| `HF_HUB_OFFLINE` | `1` (model is already cached) |

The embedding model is cached in `~/.cache/huggingface`, so the worker starts
offline in a few seconds.

---

## 2. Start it (every time)

Open four terminals in `~/code/MLSD-Mini-Project-`.

**Terminal 1 — Redis**

```bash
redis-server --daemonize yes --save '' --appendonly no --port 6379
redis-cli ping          # PONG
```

**Terminal 2 — API**

```bash
cd ~/code/MLSD-Mini-Project-
.venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --env-file .env
```

Wait for `Application startup complete`.

**Terminal 3 — worker**

```bash
cd ~/code/MLSD-Mini-Project-
.venv/bin/python -m backend.worker
```

Wait for both lines:

```
retrieval index loaded
worker listening queue=dispute-runs ...
```

**Terminal 4 — Streamlit**

```bash
cd ~/code/MLSD-Mini-Project-
API_BASE_URL=http://localhost:8000 .venv/bin/streamlit run frontend/app.py
```

Open **http://localhost:8501**.

Quick health checks:

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8000/ready
curl -s http://localhost:9100/metrics | head
```

---

## 3. Submit and view a case

In the Streamlit console:

1. Fill the form (or pick a saved query in the sidebar).
2. **Submit dispute** — status goes `queued` → `running` → `completed`.
3. The result panel shows the route, basis, citations, or the escalation reason.
4. **Recent runs** in the sidebar reopens any past run, including its trace and
   D5 reconstruction.

Or with curl:

```bash
curl -s -X POST http://localhost:8000/disputes \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' \
  -d '{
    "narrative": "A card-not-present charge I never made appeared on my card. I reported it the next day.",
    "channel": "card_not_present",
    "amount": 4999,
    "transaction_date": "2026-11-10",
    "communication_date": "2026-11-11",
    "reported_date": "2026-11-12",
    "account_type": "savings"
  }'
```

A `422` means the request was rejected before any run existed (D1): no queue job,
no model call, no cost.

---

## 4. Nothing disappears between runs

There are **three** places data is kept, on purpose:

| What | Where | Survives a reboot? |
|---|---|---|
| Live run state (queue, status, recent list) | Redis | No — Redis is started without persistence here |
| Full run result + trace | `data/runs/YYYY-MM-DD/<run_id>.json` | **Yes** |
| Every query you typed | `data/requests.jsonl` | **Yes** |

- `data/runs/` is the D5 archive. The API falls back to it when Redis has
  nothing: `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/trace` and
  `GET /runs/{id}/reconstruction` all still work after a restart or after the
  7-day Redis TTL.
- `data/requests.jsonl` is an append-only log written by the API at submit time.
  The console's **Saved queries** list reads it (`GET /requests`), so you can
  reload a previous case into the form instead of typing it again.

Back up these two paths if the demo matters:

```bash
cp -r data/runs data/requests.jsonl /some/backup/
```

There is no automatic deletion of either. Deleting them is the only way they go
away.

---

## 5. Stop

Press `Ctrl-C` in the Streamlit, worker and API terminals, then:

```bash
redis-cli shutdown nosave
```

Restarting tomorrow is just section 2 again. Because the archive and query
history are on disk, the sidebar will repopulate from `data/` even with an empty
Redis.

---

## 6. Common problems

| Symptom | Fix |
|---|---|
| `Address already in use` on 8000/8501/9100 | Something is still running: `ss -ltnp \| grep -E ':8000\|:8501\|:9100'` then kill the PID |
| `503 run queue is unavailable` | Redis is not running, or the worker was not started |
| Run stays `queued` | Worker terminal not started or still loading; wait for `worker listening` |
| `provider_unavailable` | Wrong `LLM_BASE_URL` (needs `https://api.deepseek.com/v1`) or key/quota |
| Worker exits on startup with index errors | `data/index` missing, or `HF_HUB_OFFLINE=1` without the model cached (set `0` once) |
| Want a zero-cost demo | Set `LLM_PROVIDER=fake` in `.env` and restart the worker |

---

## 7. Tests (no Redis, Docker or model needed)

```bash
cd ~/code/MLSD-Mini-Project-
.venv/bin/python -m pytest backend/tests/service -q
.venv/bin/python -m pytest backend/tests/agents  -q
```

---

## 8. Where the other docs are

- `runbook.md` — operations, scaling, failure handling, API reference
- `monitoring/SLO.md` — the declared SLOs
- `loadtest/README.md` — load test and stub LLM
- `backend/agents/README.md` — how the two agents work

`README.md` describes the full Docker stack; ignore it for the laptop demo.
