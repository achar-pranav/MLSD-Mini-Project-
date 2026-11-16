# Incident log

Operational events for the card-dispute triage service. One entry per incident
or deployment-affecting change; link the affected `run_id` and Prometheus/Jaeger
query where possible.

## 2026-11-17 — Service layer built, not yet load-tested

- Scope: FastAPI API, RQ worker, Redis run store, Prometheus metrics, OTLP
  tracing, Streamlit console, Docker Compose, stub LLM and load driver.
- Status: service tests pass; the stub conversation was verified against the
  real graph with the offline keyword retriever for `ok`, `41`, `ooc` and
  `injection`.
- Open: `monitoring/SLO.md` is declared but has no recorded measurement yet.
  The first real-provider run must be recorded under `loadtest/results/`.
- Open: the Docker image pins Python 3.12; the FAISS cp312 wheel was installed
  and imported successfully in a Python 3.12 environment, but the full image
  build must still be smoke-tested on a host with a Docker daemon.
