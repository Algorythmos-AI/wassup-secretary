"""Response schemas for the staff API: the contract the web dashboard's types are generated from.

Every response is an explicit model, so a column added to a table later can never reach a browser
by accident, and the OpenAPI document describes exactly what each endpoint returns.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["viewer", "receptionist", "admin", "owner"]
WorkflowStatus = Literal["pending", "following_up", "addressed", "no_action_needed"]


class ClinicAccess(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    timezone: str
    role: Role


class Me(BaseModel):
    email: str
    clinics: list[ClinicAccess]


class CallSummary(BaseModel):
    """One row of the call list: enough to triage, without the transcript."""

    id: uuid.UUID
    started_at: datetime | None
    from_number: str | None
    duration_seconds: int | None
    summary: str | None
    intent: str | None
    workflow_status: WorkflowStatus
    is_priority: bool
    is_reception_action: bool
    version: int


class CallPage(BaseModel):
    items: list[CallSummary]
    next_cursor: str | None


class CallRecord(CallSummary):
    provider_call_id: str
    direction: Literal["inbound", "outbound"]
    to_number: str | None
    ended_at: datetime | None
    cost_usd: str | None = Field(
        description="Voice-provider cost in US dollars, as a decimal string"
    )
    disconnection_reason: str | None
    transcript: str | None = Field(description="Plain text. Never render it as HTML or Markdown.")
    sentiment: str | None
    local_date: date | None = Field(description="Call date in the clinic's own timezone")
    local_hour: int | None
    analyzed_at: datetime | None


class CapturedMessage(BaseModel):
    category: str
    detail: str = Field(description="What the caller said. Plain text; never render as HTML.")
    callback_number: str | None
    created_at: datetime


class Interaction(BaseModel):
    action_type: str
    status_from: str | None
    status_to: str | None
    note: str | None
    created_at: datetime


class CallDetail(BaseModel):
    call: CallRecord
    messages: list[CapturedMessage]
    interactions: list[Interaction]


class WorkflowResult(BaseModel):
    call_id: uuid.UUID
    workflow_status: WorkflowStatus
    version: int


class _Dated(BaseModel):
    """Responses with a date range: ``from`` is a Python keyword, so it is aliased."""

    model_config = ConfigDict(populate_by_name=True)

    clinic_id: uuid.UUID
    from_: date = Field(alias="from")
    to: date


class Totals(BaseModel):
    calls: int
    avg_duration_seconds: int | None
    total_duration_seconds: int
    cost_usd: str
    priority: int
    reception_action: int


class DayCount(BaseModel):
    date: date
    calls: int


class HourCount(BaseModel):
    hour: int = Field(ge=0, le=23)
    calls: int


class WeekdayCount(BaseModel):
    weekday: int = Field(ge=0, le=6, description="Monday = 0")
    calls: int


class IntentCount(BaseModel):
    intent: str
    calls: int


class AnalyticsSummary(_Dated):
    timezone: str
    totals: Totals
    workflow: dict[WorkflowStatus, int]
    by_day: list[DayCount]
    by_hour: list[HourCount]
    by_weekday: list[WeekdayCount]
    sentiment: dict[str, int]
    top_intents: list[IntentCount]


class UsageDay(BaseModel):
    date: date
    calls: int
    minutes: str
    provider_cost_usd: str


class UsageTotals(BaseModel):
    calls: int
    minutes: str
    provider_cost_usd: str


class UsageReport(_Dated):
    days: list[UsageDay]
    totals: UsageTotals
