## Agent layer

Two-agent LangGraph (analysis agent + verifier) that turns a validated dispute into a verified, cited
recommendation or an escalation with a reason code. Entry point `backend/agents/graph.py:run_dispute_graph`.
Topology, tools, loop limits, escalation codes, guardrails, LLM configuration and how to run:
see [backend/agents/README.md](backend/agents/README.md).

## Service layer

FastAPI + Redis/RQ + worker + Prometheus + OpenTelemetry + Streamlit console.
`POST /disputes` is validated at the boundary (D1), queued, and executed by a worker
that persists the result, trace and metrics per `run_id` (D5).

```bash
cp .env.example .env          # set LLM_* and EMBEDDING_MODEL; .env stays untracked
docker compose up -d --build
# API: http://localhost:8000/docs · UI: http://localhost:8501
# Grafana: http://localhost:3000 · Jaeger: http://localhost:16686
```

Architecture, scaling, SLOs, run reconstruction, failure handling and rollback:
see [runbook.md](runbook.md), [monitoring/SLO.md](monitoring/SLO.md) and
[loadtest/README.md](loadtest/README.md).

Service tests need no Redis or model call:

```bash
python -m pytest backend/tests/service -q
```
