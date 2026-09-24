"""Database operations for ingestion. Every statement is parameterised."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from voice_gateway.retell import CallRecord


async def store_raw(
    conn: AsyncConnection, event: str, record: CallRecord, payload: dict[str, Any]
) -> int:
    """Persist the raw event first. A re-delivery of the same (call, event) refreshes it."""
    row = await conn.execute(
        text(
            """
            INSERT INTO retell_events_raw (event, provider_call_id, agent_id, payload)
            VALUES (:event, :call_id, :agent_id, CAST(:payload AS jsonb))
            ON CONFLICT (provider_call_id, event)
            DO UPDATE SET payload = EXCLUDED.payload, received_at = now(), error = NULL
            RETURNING id
            """
        ),
        {
            "event": event,
            "call_id": record.provider_call_id,
            "agent_id": record.agent_id,
            "payload": json.dumps(payload),
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
        {"reason": reason, "agent_id": agent_id, "payload": json.dumps(payload)},
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
