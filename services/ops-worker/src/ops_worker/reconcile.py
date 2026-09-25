"""Ingestion-gap detector (detect-only): does the voice provider have finished calls we don't?

Complements the line-check canary: the canary proves calls reach the provider; this proves the
provider's calls reach our database. It never writes calls itself — a replay from the raw event log
is an explicit, reviewed operation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import clinic_scope, unscoped
from wassup_core.logging import get_logger

from ops_worker.retell_api import RetellApi

log = get_logger(__name__)

SETTLE = timedelta(minutes=15)  # the analysed event can arrive minutes after a call ends
PROBE_LIMIT = 20


def _settled_real_calls(
    calls: list[dict[str, Any]], ai_lines: set[str], now: datetime
) -> list[str]:
    cutoff_ms = (now - SETTLE).timestamp() * 1000
    ids = []
    for c in calls:
        ended = c.get("end_timestamp")
        if (
            c.get("call_status") != "ended"
            or not isinstance(ended, int | float)
            or ended > cutoff_ms
        ):
            continue
        if c.get("direction") == "outbound" or c.get("from_number") in ai_lines:
            continue  # synthetic line checks are deliberately not stored
        if isinstance(c.get("call_id"), str):
            ids.append(c["call_id"])
    return ids


async def ingestion_gap(
    engine: AsyncEngine, retell: RetellApi, ai_lines: set[str], now: datetime
) -> dict[str, Any]:
    async with unscoped(engine) as conn:
        clinics = (await conn.execute(text("SELECT active_clinic_ids()"))).scalar() or []
    if not clinics:
        return {"ingestion": "unknown", "reason": "no_active_clinics"}
    async with clinic_scope(engine, clinics) as conn:
        agents = (
            (await conn.execute(text("SELECT agent_id FROM clinic_voice_agents WHERE active")))
            .scalars()
            .all()
        )
    if not agents:
        return {"ingestion": "unknown", "reason": "no_voice_agents"}
    try:
        calls = await retell.list_calls(list(agents), PROBE_LIMIT)
    except Exception as exc:
        log.warning("reconcile_probe_failed", code=type(exc).__name__)
        return {"ingestion": "unknown", "reason": "provider_unreachable"}
    settled = _settled_real_calls(calls, ai_lines, now)
    if not settled:
        return {"ingestion": "ok", "checked_calls": 0, "missing_calls": 0}
    async with clinic_scope(engine, clinics) as conn:
        stored = set(
            (
                await conn.execute(
                    text("SELECT provider_call_id FROM calls WHERE provider_call_id = ANY(:ids)"),
                    {"ids": settled},
                )
            )
            .scalars()
            .all()
        )
    missing = [c for c in settled if c not in stored]
    if missing:
        log.error("ingestion_gap", count=len(missing))
    return {
        "ingestion": "gap" if missing else "ok",
        "checked_calls": len(settled),
        "missing_calls": len(missing),
    }
