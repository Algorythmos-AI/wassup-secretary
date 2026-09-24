"""Daily usage rollup per clinic: calls, minutes and voice-provider cost (billing input).

Recomputes the last ``RECOMPUTE_DAYS`` clinic-local days from ``calls`` and upserts them, so a
late ``call_analyzed`` (which fills in duration and cost) or a replayed call is absorbed on the
next run. Idempotent: running it twice changes nothing. Days are the clinic's own calendar
(``calls.local_date``); line-check calls are never stored as calls, so they are never billed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import clinic_scope, unscoped
from wassup_core.logging import get_logger

log = get_logger(__name__)

RECOMPUTE_DAYS = 4

_ROLLUP = text(
    """
    INSERT INTO usage_daily (clinic_id, day, calls, minutes, provider_cost_usd)
    SELECT clinic_id, local_date, count(*),
           round(coalesce(sum(duration_seconds), 0) / 60.0, 2),
           coalesce(sum(cost_usd), 0)
    FROM calls
    WHERE local_date >= :since
    GROUP BY clinic_id, local_date
    ON CONFLICT (clinic_id, day) DO UPDATE SET
      calls = EXCLUDED.calls,
      minutes = EXCLUDED.minutes,
      provider_cost_usd = EXCLUDED.provider_cost_usd
    """
)


async def run(engine: AsyncEngine, now: datetime | None = None) -> int:
    """One pass over every active clinic. Returns the number of (clinic, day) rows written."""
    now = now or datetime.now(UTC)
    # Generous in UTC terms, so every clinic-local "last N days" is covered in any timezone.
    since = (now - timedelta(days=RECOMPUTE_DAYS)).date()
    async with unscoped(engine) as conn:
        clinics = (await conn.execute(text("SELECT active_clinic_ids()"))).scalar() or []
    if not clinics:
        return 0
    async with clinic_scope(engine, clinics) as conn:
        written = (await conn.execute(_ROLLUP, {"since": since})).rowcount
    log.info("usage_rolled_up", count=written)
    return int(written)
