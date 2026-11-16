"""OpenAI-compatible stub LLM for load tests.

The response format was the open item on the Member 3 checklist.  Rather than
inventing a second canned conversation, this server reuses Member 2's
``ScriptedAgents`` — the same scripted behaviour used by ``LLM_PROVIDER=fake``.
It therefore returns a real tool call, receives the real tool result, and then
returns the final JSON answer the graph accepts.

Run:
    uvicorn loadtest.stub_llm:app --host 0.0.0.0 --port 9000

Point the worker at it with:
    LLM_PROVIDER=openai
    LLM_BASE_URL=http://stub-llm:9000/v1
    LLM_MODEL=stub
    LLM_API_KEY=stub
"""
from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.agents.fake_scenarios import ScriptedAgents
from backend.agents.llm import LLMResponse

logger = logging.getLogger("dispute.stub_llm")
app = FastAPI(title="Dispute stub LLM", version="1")

_agents = ScriptedAgents()
_counter = itertools.count(1)
_stats_lock = threading.Lock()
_stats = {"requests": 0, "tool_calls": 0, "final_answers": 0, "errors": 0}


def _usage(body: dict[str, Any], content: str | None) -> dict[str, int]:
    prompt_chars = len(json.dumps(body.get("messages") or []))
    completion_chars = len(content or "")
    prompt = max(1, prompt_chars // 4)
    completion = max(1, completion_chars // 4)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _completion(body: dict[str, Any], message: dict[str, Any], finish_reason: str,
                content: str | None) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-stub-{next(_counter)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model") or "stub",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": _usage(body, content),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": "stub", "object": "model"}]}


@app.get("/stats")
def stats() -> dict[str, int]:
    with _stats_lock:
        return dict(_stats)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    tools = body.get("tools")
    with _stats_lock:
        _stats["requests"] += 1
    if not messages:
        with _stats_lock:
            _stats["errors"] += 1
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "messages is required", "type": "invalid_request"}},
        )
    try:
        response: LLMResponse = _agents(messages, tools)
    except Exception as exc:  # noqa: BLE001 - mirror a provider 5xx, not a crash
        with _stats_lock:
            _stats["errors"] += 1
        logger.warning("stub failed: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=503,
            content={"error": {"message": f"stub error: {exc}", "type": "provider_error"}},
        )

    if response.tool_calls:
        calls = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in response.tool_calls
        ]
        with _stats_lock:
            _stats["tool_calls"] += 1
        return _completion(
            body,
            {"role": "assistant", "content": None, "tool_calls": calls},
            "tool_calls",
            None,
        )

    content = response.content or ""
    with _stats_lock:
        _stats["final_answers"] += 1
    return _completion(body, {"role": "assistant", "content": content}, "stop", content)
