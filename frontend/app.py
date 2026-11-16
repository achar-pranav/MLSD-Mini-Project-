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
    st.header("Saved queries")
    try:
        history = httpx.get(
            f"{API_BASE_URL}/requests", params={"limit": 20}, timeout=5,
        ).json()
        saved = history.get("requests", [])
    except (httpx.HTTPError, ValueError):
        saved = []

    def saved_label(entry: dict) -> str:
        request = entry.get("request") or {}
        text = (request.get("narrative") or "").strip().replace("\n", " ")
        return f"{str(entry.get('submitted_at'))[:16]} · {text[:38]}…"

    if saved:
        pick = st.selectbox(
            "Reuse a query", options=range(len(saved)), format_func=lambda i: saved_label(saved[i]),
        )
        if st.button("Load into form", use_container_width=True):
            st.session_state["prefill"] = saved[pick]["request"]
            st.session_state["prefill_nonce"] = int(st.session_state.get("prefill_nonce", 0)) + 1
            st.rerun()
    else:
        st.caption("No saved queries yet.")

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

prefill = st.session_state.get("prefill") or {}
nonce = int(st.session_state.get("prefill_nonce", 0))


def _prefill_date(value: object, fallback: date) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback


account_options = ["savings", "current", "credit_card", "other"]
channel_default = prefill.get("channel") if prefill.get("channel") in CHANNELS else CHANNELS[0]
account_default = (
    prefill.get("account_type") if prefill.get("account_type") in account_options else "savings"
)

with st.form(f"dispute_{nonce}"):
    narrative = st.text_area(
        "What happened? (the customer's own words)",
        value=prefill.get("narrative") or "",
        height=150,
        placeholder="A card-not-present charge I never made appeared on my card...",
        key=f"narrative_{nonce}",
    )
    left, middle, right = st.columns(3)
    channel = left.selectbox(
        "Channel", CHANNELS, index=CHANNELS.index(channel_default), key=f"channel_{nonce}",
    )
    amount = middle.number_input(
        "Amount (INR)", min_value=1.0, value=float(prefill.get("amount") or 4999.0),
        step=100.0, key=f"amount_{nonce}",
    )
    account_type = right.selectbox(
        "Account type", account_options, index=account_options.index(account_default),
        key=f"account_{nonce}",
    )
    merchant = st.text_input(
        "Merchant (optional)", value=prefill.get("merchant") or "", key=f"merchant_{nonce}",
    )
    alert_known = st.checkbox(
        "I know when the bank's alert reached the customer",
        value=bool(prefill.get("communication_date")), key=f"alert_{nonce}",
    )
    d1, d2, d3 = st.columns(3)
    transaction_date = d1.date_input(
        "Transaction date",
        value=_prefill_date(prefill.get("transaction_date"), date.today() - timedelta(days=10)),
        key=f"td_{nonce}",
    )
    communication_date = d2.date_input(
        "Bank alert date",
        value=_prefill_date(prefill.get("communication_date"), date.today() - timedelta(days=10)),
        disabled=not alert_known,
        key=f"cd_{nonce}",
    )
    reported_date = d3.date_input(
        "Reported date",
        value=_prefill_date(prefill.get("reported_date"), date.today()),
        key=f"rd_{nonce}",
    )
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
