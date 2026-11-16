# Service-level objectives (declared before measurement)

Declared: **2026-11-17**. Owner: Member 3 (service layer).
Status: **declared, not yet measured on this commit.** A measurement is only
valid if it is recorded under `loadtest/results/` and the command is quoted.

## Definitions

A run starts when `POST /disputes` returns `202` and ends when
`GET /runs/{run_id}` first reports `completed` or `failed`. Queue wait is
included. `escalated` is a **valid outcome**, not a failure: the agent layer is
designed to escalate rather than guess. `failed` means the service could not
produce a `DisputeResult` (infrastructure error).

Two profiles are declared because free-tier provider pacing dominates latency:

| Profile | LLM | Purpose |
|---|---|---|
| `real` | configured OpenAI-compatible provider | production SLO |
| `stub` | `loadtest/stub_llm.py` (same scripted conversation as `LLM_PROVIDER=fake`) | service and queue SLO |

## SLOs

### Real provider profile

- **SLO-R1 (latency):** p95 run duration ≤ **120 s**, p99 ≤ **180 s**.
  A normal run is 4–5 model calls and 30–90 s on a free tier; 120 s is the
  declared ceiling, not a measured value.
- **SLO-R2 (reliability):** infrastructure failure rate ≤ **1%** over a rolling
  30 days.
- **SLO-R3 (provider degradation):** `provider_unavailable` escalations ≤ **5%**
  of runs over a rolling 30 days. A spike is an incident, not a code bug.
- **Measurement:** at least 50 runs, `LLM_MIN_INTERVAL_S=0`, one worker,
  warm index, provider not rate-limited. Record provider and date.

### Stub profile (regression/load gate)

- **SLO-S1 (completion):** p95 run duration ≤ **15 s** at **10 concurrent**
  runs, 0.5 s polling, 1 worker.
- **SLO-S2 (submission):** p95 `POST /disputes` ≤ **500 ms**.
- **SLO-S3 (reliability):** failure rate < **0.5%**.
- **SLO-S4 (correctness of the harness):** the `injection` scenario returns
  `injection_detected` with no model call; the `ooc` scenario returns
  `out_of_corpus`. Other scenarios return either `recommended` or a justified
  escalation. A harness that turns every case into `verification_failed` is
  invalid.

## Prometheus mapping

| SLO | Metric |
|---|---|
| R1 / S1 | `histogram_quantile(0.95, sum(rate(dispute_run_duration_seconds_bucket[10m])) by (le))` |
| R2 / S3 | `sum(rate(dispute_runs_total{outcome="failed"}[10m])) / sum(rate(dispute_runs_total[10m]))` |
| R3 | `sum(rate(dispute_runs_total{reason_code="provider_unavailable"}[10m]))` |
| S2 | `histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{path="/disputes"}[5m])) by (le))` |
| all | `dispute_queue_depth`, `dispute_run_model_calls`, `dispute_run_tool_calls`, `dispute_run_retries` |

Alert rules live in `monitoring/alerts.yml`.

## How to measure

```bash
# Stub profile (no provider quota consumed)
docker compose -f docker-compose.yml -f docker-compose.loadtest.yml up -d --build
python loadtest/load.py --runs 50 --concurrency 10
# -> writes loadtest/results/load-*.json and exits non-zero on a breach

# Real profile: use the normal compose file with provider credentials in .env
python loadtest/load.py --runs 50 --concurrency 1 \
  --slo-completion-p95-ms 120000 --slo-submit-p95-ms 1000 \
  --mix ok,41,ooc
```

Do not edit the thresholds after seeing a result. If a target changes, record a
new declaration date and keep the old measurement.
