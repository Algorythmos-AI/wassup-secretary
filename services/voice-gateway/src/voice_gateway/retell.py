"""Mapping of Retell call payloads (public API shape) to our call record. Pure functions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

ANALYZED_EVENT = "call_analyzed"
KNOWN_EVENTS = frozenset({"call_started", "call_ended", ANALYZED_EVENT})


class PayloadError(ValueError):
    """The request is signed but not a usable Retell event."""


@dataclass(frozen=True)
class CallRecord:
    provider_call_id: str
    agent_id: str | None
    direction: str
    from_number: str | None
    to_number: str | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    cost_usd: Decimal | None
    disconnection_reason: str | None
    summary: str | None
    transcript: str | None
    sentiment: str | None
    intent: str | None
    analyzed: bool

    def local_fields(self, timezone: str) -> tuple[Any, int | None, int | None]:
        """Calendar date, hour and weekday (Mon=0) of the call start in the clinic's timezone."""
        if self.started_at is None:
            return None, None, None
        local = self.started_at.astimezone(ZoneInfo(timezone))
        return local.date(), local.hour, local.weekday()


def _str(value: Any, max_len: int = 200_000) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:max_len] if value else None


def _ts(ms: Any) -> datetime | None:
    if isinstance(ms, int | float) and ms > 0:
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    return None


def parse_event(payload: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise PayloadError("payload must be an object")
    event = payload.get("event")
    call = payload.get("call")
    if not isinstance(event, str) or not isinstance(call, dict):
        raise PayloadError("missing event or call")
    if not isinstance(call.get("call_id"), str) or not call["call_id"]:
        raise PayloadError("missing call.call_id")
    return event, call


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def to_record(event: str, call: dict[str, Any]) -> CallRecord:
    analysis = _obj(call.get("call_analysis"))
    custom = _obj(analysis.get("custom_analysis_data"))
    cost = _obj(call.get("call_cost"))
    combined_cents = cost.get("combined_cost")
    duration_ms = call.get("duration_ms")
    raw_direction = call.get("direction")
    direction: str = raw_direction if raw_direction in ("inbound", "outbound") else "inbound"
    analyzed = event == ANALYZED_EVENT
    return CallRecord(
        provider_call_id=call["call_id"],
        agent_id=_str(call.get("agent_id"), 200),
        direction=direction,
        from_number=_str(call.get("from_number"), 32),
        to_number=_str(call.get("to_number"), 32),
        started_at=_ts(call.get("start_timestamp")),
        ended_at=_ts(call.get("end_timestamp")),
        duration_seconds=round(duration_ms / 1000)
        if isinstance(duration_ms, int | float)
        else None,
        # Retell reports cost in cents; we store dollars.
        cost_usd=(Decimal(str(combined_cents)) / 100).quantize(Decimal("0.0001"))
        if isinstance(combined_cents, int | float)
        else None,
        disconnection_reason=_str(call.get("disconnection_reason"), 100),
        summary=_str(analysis.get("call_summary"), 8_000) if analyzed else None,
        transcript=_str(call.get("transcript")),
        sentiment=_str(analysis.get("user_sentiment"), 50) if analyzed else None,
        intent=_str(custom.get("intent"), 100) if analyzed else None,
        analyzed=analyzed,
    )
