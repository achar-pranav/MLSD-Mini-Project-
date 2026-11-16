"""Streamlit analyst console for the card-dispute triage service.

Run locally:
    API_BASE_URL=http://localhost:8000 streamlit run frontend/app.py

The UI deliberately shows only what the agent layer allows:
* recommended -> route, basis, citations
* escalated   -> reason code, note, competing provisions; unverified drafts are
                 shown separately from the trace and labelled NOT VERIFIED.
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta

import httpx
import pandas as pd
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
POLL_TIMEOUT_S = int(os.getenv("POLL_TIMEOUT_S", "180"))
POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "2"))

CHANNELS = ["card_not_present", "card_present", "atm", "upi", "merchant", "other"]

REASON_HELP = {
    "contested_fault": "The facts do not settle who was at fault.",
    "conflicting_provisions": "Two provisions apply and point different ways.",
    "out_of_corpus": "The corpus does not cover this kind of loss.",
    "verification_failed": "The draft answer could not be supported by retrieved text.",
    "budget_exhausted": "The run hit its model-call, tool-call or turn limit.",
    "timeout": "The run hit the wall-clock deadline.",
    "provider_unavailable": "The model provider was down or the circuit breaker was open.",
    "injection_detected": "Prompt injection was found in free text; no model was called.",
}

st.set_page_config(page_title="Card Dispute Triage", page_icon="💳", layout="wide")


@st.cache_data(ttl=5)
def api_health() -> bool:
    try:
        return httpx.get(f"{API_BASE_URL}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def api_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:500]}"
    detail = body.get("detail")
    if isinstance(detail, list):
        lines = []
        for item in detail:
            loc = ".".join(str(part) for part in item.get("loc", []) if part != "body")
            lines.append(f"{loc}: {item.get('msg', 'invalid value')}")
        return "; ".join(lines)
    return str(detail or body)


def submit(payload: dict) -> dict:
    headers = {"Idempotency-Key": f"ui-{int(time.time() * 1000)}"}
    response = httpx.post(
        f"{API_BASE_URL}/disputes", json=payload, headers=headers, timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(api_error(response))
    return response.json()


def wait_for_run(run_id: str) -> dict:
    deadline = time.time() + POLL_TIMEOUT_S
    state = "queued"
    while time.time() < deadline:
        response = httpx.get(f"{API_BASE_URL}/runs/{run_id}", timeout=15)
        if response.status_code >= 400:
            raise RuntimeError(api_error(response))
        run = response.json()
        if run["status"] in {"completed", "failed"}:
            return run
        if run["status"] != state:
            state = run["status"]
            st.write(f"Status: **{state}**")
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"run {run_id} did not finish within {POLL_TIMEOUT_S}s")


def render_citations(citations: list[dict]) -> None:
    if not citations:
        st.info("No citations.")
        return
    for citation in citations:
        with st.expander(f"{citation.get('doc_id')} — {citation.get('clause_id')}"):
            st.markdown(f"> {citation.get('quote', '')}")


def render_result(run: dict) -> None:
    result = run.get("result") or {}
    if run["status"] == "failed":
        st.error(f"Infrastructure failure: {run.get('error') or 'unknown error'}")
        return
    if not result:
        st.warning("Run has no result.")
        return

    status = result.get("status")
    classification = result.get("classification")
    delay = result.get("reporting_delay_working_days")
    meta = " · ".join(
        value for value in [
            f"classification: {classification}" if classification else "",
            f"reporting delay: {delay} working days" if delay is not None else "",
            f"prompt: {result.get('prompt_version')}" if result.get("prompt_version") else "",
        ] if value
    )
    if meta:
        st.caption(meta)

    if status == "recommended":
        st.success(f"Recommended route: {result.get('route')}")
        st.subheader("Basis")
        st.write(result.get("basis") or "—")
        st.subheader("Liability position")
        st.write(result.get("liability_position") or "—")
        st.subheader("Citations")
        render_citations(result.get("citations") or [])
        verification = result.get("verification") or {}
        st.caption(
            f"Verification passed · attempts: {verification.get('attempts', 0)}"
        )
    else:
        escalation = result.get("escalation") or {}
        reason = escalation.get("reason_code", "unknown")
        st.warning(f"Escalated: {reason}")
        st.caption(REASON_HELP.get(reason, "Escalation."))
        st.subheader("Note")
        st.write(escalation.get("note") or "—")
        competing = escalation.get("competing_provisions") or []
        if competing:
            st.subheader("Competing provisions")
            st.write(competing)
        citations = result.get("citations") or []
        if citations:
            st.subheader("Citations")
            render_citations(citations)
        if not citations:
            st.caption("Unverified escalations intentionally carry no basis or citations.")

    trace = run.get("trace") or result.get("trace") or []
    draft = next(
        (entry for entry in trace if entry.get("name") == "escalate.unverified_draft"),
        None,
    )
    if draft:
        with st.expander("NOT VERIFIED — model draft (from the trace)"):
            st.warning(draft.get("detail", {}).get("warning", "NOT VERIFIED"))
            st.json(draft.get("detail", {}))

    if trace:
        st.subheader("Trace")
        frame = pd.DataFrame([
            {
                "type": entry.get("type"),
                "name": entry.get("name"),
                "latency_ms": round(float(entry.get("latency_ms") or 0), 1),
                "prompt_tokens": entry.get("prompt_tokens", 0),
                "completion_tokens": entry.get("completion_tokens", 0),
                "cost_usd": round(float(entry.get("cost_usd") or 0), 6),
            }
            for entry in trace
        ])
        st.dataframe(frame, use_container_width=True, hide_index=True)
        with st.expander("Full trace (JSON)"):
            st.json(trace)
        with st.expander("D5 reconstruction"):
            try:
                recon = httpx.get(
                    f"{API_BASE_URL}/runs/{run['run_id']}/reconstruction", timeout=15,
                )
                if recon.status_code == 200:
                    st.json(recon.json())
                else:
                    st.error(api_error(recon))
            except httpx.HTTPError as exc:
                st.error(f"Could not load reconstruction: {exc}")


# --------------------------------------------------------------------------- #
# sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Service")
    healthy = api_health()
    st.write("API:", "🟢 healthy" if healthy else "🔴 unreachable")
    st.caption(API_BASE_URL)
    st.divider()
    st.header("Recent runs")
    try:
        recent = httpx.get(f"{API_BASE_URL}/runs", params={"limit": 10}, timeout=5).json()
        runs = recent.get("runs", [])
    except (httpx.HTTPError, ValueError):
        runs = []
    if runs:
        labels = [
            f"{r['run_id'][:18]} · {r['status']} · {r.get('outcome') or '—'}"
            for r in runs
        ]
        choice = st.selectbox("Select a run", options=range(len(runs)), format_func=lambda i: labels[i])
        if st.button("Load selected run", use_container_width=True):
            try:
                selected = httpx.get(
                    f"{API_BASE_URL}/runs/{runs[choice]['run_id']}", timeout=15,
                ).json()
                st.session_state["loaded_run"] = selected
            except httpx.HTTPError as exc:
                st.error(f"Could not load run: {exc}")
    else:
        st.caption("No runs yet.")

# --------------------------------------------------------------------------- #
# submission form
# --------------------------------------------------------------------------- #
st.title("Card Dispute Triage")
st.caption("Two-agent review over RBI and SBI provisions. Recommendations are verified; escalations are explicit.")

with st.form("dispute"):
    narrative = st.text_area(
        "What happened? (the customer's own words)",
        height=150,
        placeholder="A card-not-present charge I never made appeared on my card...",
    )
    left, middle, right = st.columns(3)
    channel = left.selectbox("Channel", CHANNELS)
    amount = middle.number_input("Amount (INR)", min_value=1.0, value=4999.0, step=100.0)
    account_type = right.selectbox(
        "Account type", ["savings", "current", "credit_card", "other"],
    )
    merchant = st.text_input("Merchant (optional)")
    alert_known = st.checkbox("I know when the bank's alert reached the customer", value=True)
    d1, d2, d3 = st.columns(3)
    transaction_date = d1.date_input("Transaction date", value=date.today() - timedelta(days=10))
    communication_date = d2.date_input(
        "Bank alert date", value=date.today() - timedelta(days=10), disabled=not alert_known,
    )
    reported_date = d3.date_input("Reported date", value=date.today())
    submitted = st.form_submit_button("Submit dispute", type="primary")

if submitted:
    payload = {
        "narrative": narrative,
        "channel": channel,
        "amount": amount,
        "transaction_date": transaction_date.isoformat(),
        "communication_date": communication_date.isoformat() if alert_known else None,
        "reported_date": reported_date.isoformat(),
        "merchant": merchant or None,
        "account_type": account_type,
    }
    try:
        accepted = submit(payload)
    except (RuntimeError, httpx.HTTPError) as exc:
        st.error(f"Request rejected: {exc}")
    else:
        st.info(f"Run `{accepted['run_id']}` queued. Waiting for the worker…")
        try:
            with st.status("Running the two-agent graph…", expanded=True) as status:
                run = wait_for_run(accepted["run_id"])
                status.update(label=f"Run {run['status']}", state="complete")
        except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
            st.error(str(exc))
        else:
            st.session_state["loaded_run"] = run

loaded = st.session_state.get("loaded_run")
if loaded:
    st.divider()
    st.subheader(f"Run {loaded['run_id']}")
    st.caption(
        f"status: {loaded['status']} · correlation: {loaded.get('correlation_id')} · "
        f"duration: {loaded.get('duration_s') or '—'} s"
    )
    render_result(loaded)
