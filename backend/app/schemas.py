"""Public request and response schemas for the dispute API.

Requirement D1 lives here: every rule that can reject a request *before* a run
is charged to the queue belongs in :class:`DisputeRequest`.  The agent layer's
``DisputeInput`` declares the same fields but only shapes, not business rules.

Deliberate non-goal: prompt-injection text is NOT rejected here.  The graph
scans free text before any model call and returns an ``escalation`` with
``reason_code="injection_detected"`` (see ``backend/agents/guardrails.py``).
Rejecting it at the boundary would hide that behaviour from the UI and tests.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.agents.state import DisputeInput, DisputeResult, ReasonCode, TraceEntry

RunStatus = Literal["queued", "running", "completed", "failed"]


class DisputeRequest(BaseModel):
    """The request contract.  Mirrors ``backend.agents.state.DisputeInput``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    narrative: str = Field(
        ..., min_length=10, max_length=5000,
        description="The customer's own words. Untrusted free text, never instructions.",
    )
    channel: str = Field(
        ..., min_length=1, max_length=64,
        description="Channel label, e.g. card_not_present, card_present, atm, upi.",
    )
    amount: float = Field(..., gt=0, le=10_000_000, allow_inf_nan=False)
    transaction_date: date
    communication_date: date | None = Field(
        default=None,
        description="When the bank's SMS/e-mail alert reached the customer, if known.",
    )
    reported_date: date
    merchant: str | None = Field(default=None, max_length=200)
    account_type: str | None = Field(default=None, max_length=64)

    @field_validator("channel")
    @classmethod
    def _channel_is_printable(cls, value: str) -> str:
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("channel must not contain control characters")
        return value

    @field_validator("merchant", "account_type", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def _date_rules(self) -> "DisputeRequest":
        today = date.today()
        if self.transaction_date > today:
            raise ValueError("transaction_date cannot be in the future")
        if self.reported_date < self.transaction_date:
            raise ValueError("reported_date cannot be before transaction_date")
        if self.reported_date > today:
            raise ValueError("reported_date cannot be in the future")
        if self.communication_date is not None:
            if self.communication_date < self.transaction_date:
                raise ValueError("communication_date cannot be before transaction_date")
            if self.communication_date > today:
                raise ValueError("communication_date cannot be in the future")
        return self

    def to_agent_input(self) -> DisputeInput:
        """Convert to the agent layer's input type without changing any value."""
        return DisputeInput.model_validate(self.model_dump())


class RunSubmission(BaseModel):
    run_id: str
    correlation_id: str
    status: RunStatus
    created_at: datetime
    idempotent_replay: bool = False


class RunView(BaseModel):
    run_id: str
    correlation_id: str
    status: RunStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_s: float | None = None
    request: DisputeRequest
    result: DisputeResult | None = None
    trace: list[TraceEntry] | None = None
    error: str | None = None
    worker: str | None = None


class RunSummary(BaseModel):
    run_id: str
    status: RunStatus
    outcome: Literal["recommended", "escalated", "failed"] | None = None
    reason_code: ReasonCode | None = None
    route: str | None = None
    classification: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    duration_s: float | None = None


class RunList(BaseModel):
    runs: list[RunSummary]
    count: int


class RunReconstruction(BaseModel):
    """D5: everything needed to explain one run after the fact."""

    run_id: str
    correlation_id: str
    status: RunStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_s: float | None = None
    request: DisputeRequest
    result: DisputeResult | None = None
    trace: list[TraceEntry]
    error: str | None = None
    worker: str | None = None
    service: dict[str, Any] = Field(default_factory=dict)


class Health(BaseModel):
    status: Literal["ok"]
    service: str
    version: str


class Readiness(BaseModel):
    status: Literal["ready"]
    store: bool
    index: bool
    queue: bool
