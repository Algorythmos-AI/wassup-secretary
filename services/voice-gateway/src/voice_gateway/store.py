"""Database operations for ingestion. Every statement is parameterised."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from voice_gateway.retell import CallRecord


def strip_nul(value: Any) -> Any:
    """Postgres text and jsonb reject NUL characters; a caller's words must never make a write
    fail, so they are removed everywhere before storage."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_nul(v) for v in value]
    return value


def dumps_clean(payload: Any) -> str:
    return json.dumps(strip_nul(payload))


async def store_tool_request(
    conn: AsyncConnection,
    *,
    dedupe_key: str,
    tool: str,
    clinic_slug: str,
    call_id: str,
    agent_id: str | None,
    payload: dict[str, Any],
) -> None:
    """Commit a write-tool request before running it, so a timeout can't lose what was said."""
    await conn.execute(
        text(
            """
            INSERT INTO tool_requests_raw (dedupe_key, tool, clinic_slug, provider_call_id, agent_id, payload)
            VALUES (:key, :tool, :slug, :call_id, :agent_id, CAST(:payload AS jsonb))
            ON CONFLICT (dedupe_key) DO NOTHING
            """
        ),
        {
            "key": dedupe_key,
            "tool": tool,
            "slug": clinic_slug,
            "call_id": call_id,
            "agent_id": agent_id,
            "payload": dumps_clean(payload),
        },
    )


async def complete_tool_request(
    conn: AsyncConnection, dedupe_key: str, outcome: str, clinic_id: uuid.UUID | None = None
) -> None:
    await conn.execute(
        text(
            """
            UPDATE tool_requests_raw
            SET completed_at = now(), outcome = :outcome, clinic_id = COALESCE(CAST(:clinic_id AS uuid), clinic_id)
            WHERE dedupe_key = :key AND completed_at IS NULL
            """
        ),
        {"key": dedupe_key, "outcome": outcome, "clinic_id": clinic_id},
    )


# A line check is a call we placed ourselves, between our own lines. Caller ID can be spoofed,
# so "from one of our numbers" alone is not proof: a run for that exact pair must have been
# started in the last 30 minutes, otherwise it is treated as a real (patient) call.
_CANARY_PAIR = text(
    """
    SELECT 1 FROM canary_runs
    WHERE from_number = :from_number AND to_number = :to_number
      AND COALESCE(placed_at, created_at) > now() - interval '30 minutes'
    LIMIT 1
    """
)


async def is_synthetic(
    conn: AsyncConnection, call: dict[str, Any], ai_lines: frozenset[str]
) -> bool:
    if call.get("direction") == "outbound":  # only the line check places outbound calls
        return True
    from_number, to_number = call.get("from_number"), call.get("to_number")
    if not isinstance(from_number, str) or from_number not in ai_lines:
        return False
    found = await conn.execute(_CANARY_PAIR, {"from_number": from_number, "to_number": to_number})
    return found.first() is not None


async def store_raw(
    conn: AsyncConnection, event: str, record: CallRecord, payload: dict[str, Any]
) -> int:
    """Persist the raw event first. A re-delivery (Retell retry or our own replay) refreshes the
    payload and clears the error; ``received_at`` keeps the first receipt, which replay backoff
    and the stuck-event monitor are measured from."""
    row = await conn.execute(
        text(
            """
            INSERT INTO retell_events_raw (event, provider_call_id, agent_id, payload)
            VALUES (:event, :call_id, :agent_id, CAST(:payload AS jsonb))
            ON CONFLICT (provider_call_id, event)
            DO UPDATE SET payload = EXCLUDED.payload, error = NULL
            RETURNING id
            """
        ),
        {
            "event": event,
            "call_id": record.provider_call_id,
            "agent_id": record.agent_id,
            "payload": dumps_clean(payload),
        },
    )
    return int(row.scalar_one())


async def mark_raw(
    conn: AsyncConnection,
    raw_id: int,
    *,
    clinic_id: uuid.UUID | None = None,
    error: str | None = None,
) -> None:
    await conn.execute(
        text(
            """
            UPDATE retell_events_raw
            SET processed_at = CASE WHEN CAST(:error AS text) IS NULL THEN now() ELSE processed_at END,
                clinic_id = COALESCE(CAST(:clinic_id AS uuid), clinic_id),
                error = CAST(:error AS text)
            WHERE id = :id
            """
        ),
        {"id": raw_id, "clinic_id": clinic_id, "error": error},
    )


async def quarantine(
    conn: AsyncConnection, reason: str, agent_id: str | None, payload: dict[str, Any]
) -> None:
    await conn.execute(
        text(
            "INSERT INTO quarantine_events (reason, agent_id, payload) "
            "VALUES (:reason, :agent_id, CAST(:payload AS jsonb))"
        ),
        {"reason": reason, "agent_id": agent_id, "payload": dumps_clean(payload)},
    )


async def resolve_clinic(
    conn: AsyncConnection, agent_id: str | None, dialled: str | None
) -> uuid.UUID | None:
    if not agent_id:
        return None
    value = await conn.execute(
        text("SELECT resolve_clinic_for_call(:agent_id, :dialled)"),
        {"agent_id": agent_id, "dialled": dialled},
    )
    result = value.scalar()
    return uuid.UUID(str(result)) if result is not None else None


async def clinic_timezone(conn: AsyncConnection, clinic_id: uuid.UUID) -> str:
    value = await conn.execute(
        text("SELECT timezone FROM clinics WHERE id = :id"), {"id": clinic_id}
    )
    return str(value.scalar_one())


# Later events never erase earlier facts, and only an analysed event may set analysis fields.
_UPSERT_CALL = text(
    """
    INSERT INTO calls (
      clinic_id, provider_call_id, direction, from_number, to_number, started_at, ended_at,
      duration_seconds, cost_usd, disconnection_reason, summary, transcript, sentiment, intent,
      local_date, local_hour, local_dow, source, analyzed_at
    ) VALUES (
      :clinic_id, :provider_call_id, :direction, :from_number, :to_number, :started_at, :ended_at,
      :duration_seconds, :cost_usd, :disconnection_reason, :summary, :transcript, :sentiment, :intent,
      :local_date, :local_hour, :local_dow, 'webhook', CASE WHEN CAST(:analyzed AS boolean) THEN now() END
    )
    ON CONFLICT (clinic_id, provider_call_id) DO UPDATE SET
      from_number          = COALESCE(calls.from_number, EXCLUDED.from_number),
      to_number            = COALESCE(calls.to_number, EXCLUDED.to_number),
      started_at           = COALESCE(calls.started_at, EXCLUDED.started_at),
      local_date           = COALESCE(calls.local_date, EXCLUDED.local_date),
      local_hour           = COALESCE(calls.local_hour, EXCLUDED.local_hour),
      local_dow            = COALESCE(calls.local_dow, EXCLUDED.local_dow),
      ended_at             = COALESCE(EXCLUDED.ended_at, calls.ended_at),
      duration_seconds     = COALESCE(EXCLUDED.duration_seconds, calls.duration_seconds),
      cost_usd             = COALESCE(EXCLUDED.cost_usd, calls.cost_usd),
      disconnection_reason = COALESCE(EXCLUDED.disconnection_reason, calls.disconnection_reason),
      transcript           = COALESCE(EXCLUDED.transcript, calls.transcript),
      summary   = CASE WHEN CAST(:analyzed AS boolean) THEN EXCLUDED.summary   ELSE calls.summary END,
      sentiment = CASE WHEN CAST(:analyzed AS boolean) THEN EXCLUDED.sentiment ELSE calls.sentiment END,
      intent    = CASE WHEN CAST(:analyzed AS boolean) THEN EXCLUDED.intent    ELSE calls.intent END,
      analyzed_at = CASE WHEN CAST(:analyzed AS boolean) THEN now() ELSE calls.analyzed_at END,
      updated_at  = now()
    RETURNING id
    """
)


async def upsert_call(
    conn: AsyncConnection, clinic_id: uuid.UUID, record: CallRecord, timezone: str
) -> uuid.UUID:
    local_date, local_hour, local_dow = record.local_fields(timezone)
    row = await conn.execute(
        _UPSERT_CALL,
        {
            "clinic_id": clinic_id,
            "provider_call_id": record.provider_call_id,
            "direction": record.direction,
            "from_number": record.from_number,
            "to_number": record.to_number,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "duration_seconds": record.duration_seconds,
            "cost_usd": record.cost_usd,
            "disconnection_reason": record.disconnection_reason,
            "summary": record.summary,
            "transcript": record.transcript,
            "sentiment": record.sentiment,
            "intent": record.intent,
            "local_date": local_date,
            "local_hour": local_hour,
            "local_dow": local_dow,
            "analyzed": record.analyzed,
        },
    )
    return uuid.UUID(str(row.scalar_one()))


async def enqueue(
    conn: AsyncConnection,
    clinic_id: uuid.UUID,
    event_type: str,
    dedupe_key: str,
    payload: dict[str, Any],
) -> None:
    """Outbox row in the same transaction as the data change. Ids only, never personal data."""
    await conn.execute(
        text(
            """
            INSERT INTO outbox_events (clinic_id, event_type, dedupe_key, payload)
            VALUES (:clinic_id, :event_type, :dedupe_key, CAST(:payload AS jsonb))
            ON CONFLICT (dedupe_key) DO NOTHING
            """
        ),
        {
            "clinic_id": clinic_id,
            "event_type": event_type,
            "dedupe_key": dedupe_key,
            "payload": json.dumps(payload),
        },
    )


async def record_canary_receipt(conn: AsyncConnection, to_number: str | None) -> bool:
    """A synthetic inbound call arrived: mark the newest open line-check run for that line."""
    if not to_number:
        return False
    row = await conn.execute(
        text(
            """
            UPDATE canary_runs
            SET received_at = now(), status = CASE WHEN status = 'failed' THEN 'late' ELSE 'received' END
            WHERE id = (
              SELECT id FROM canary_runs
              WHERE to_number = :to AND received_at IS NULL AND placed_at IS NOT NULL
              ORDER BY placed_at DESC LIMIT 1
            )
            RETURNING id
            """
        ),
        {"to": to_number},
    )
    return row.scalar() is not None
