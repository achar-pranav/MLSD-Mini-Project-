# Load test and stub LLM

## The canned conversation (the open item)

`loadtest/stub_llm.py` is an HTTP server that implements
`POST /v1/chat/completions`.  It reuses `backend/agents/fake_scenarios.py`
(`ScriptedAgents`) instead of defining a second script:

1. **First turn** — the stub returns an OpenAI `tool_calls` response for
   `search_corpus`.
2. **After the tool result** — it returns the final analyst JSON.
3. **Modifier check / verifier** — it recognises the conversation and returns
   the verifier JSON.

That is the format the graph already accepts under `LLM_PROVIDER=fake`.  If
Member 2 changes `ScriptedAgents`, the stub follows automatically.

## Run it

```bash
# Build the service image once; the overlay points the worker at the stub.
docker compose -f docker-compose.yml -f docker-compose.loadtest.yml up -d --build
curl -s localhost:9000/health
curl -s localhost:9000/stats

# warm the index/model and the stub before measuring
python loadtest/load.py --runs 2 --concurrency 1 --mix ok
python loadtest/load.py --runs 50 --concurrency 10
```

The driver submits runs, polls until they finish and writes
`loadtest/results/load-YYYYmmdd-HHMMSS.json`.  It exits non-zero if the declared
SLO thresholds in `monitoring/SLO.md` are breached.

Useful options:

```bash
python loadtest/load.py \
  --base-url http://localhost:8000 \
  --runs 50 --concurrency 10 \
  --mix ok,41,ooc,injection \
  --poll-timeout 180 \
  --slo-completion-p95-ms 15000
```

## What to check in the result

- `error_rate` (must be below 0.5% in the stub profile)
- `completion_ms_p95` (must be ≤ 15 s)
- `by_reason`: `out_of_corpus` for `ooc`, `injection_detected` for `injection`
- `stats` on the stub: run an injection-only batch and confirm `requests` does
  **not** increase (the graph escalates before any model call):

  ```bash
  curl -s localhost:9000/stats
  python loadtest/load.py --runs 5 --concurrency 1 --mix injection
  curl -s localhost:9000/stats
  ```

## Notes

- The stub is never enabled by the default compose file.
- It is a test harness, not a model. It does not test answer quality; that is
  `evaluation/evaluate.py` (Member 2).
- Restart the worker after changing the overlay so it picks up `LLM_BASE_URL`.
