"""Deletes raw provider payloads once they are finished with and older than the retention period.

Raw payloads duplicate what the call records already hold (transcripts, messages); keeping them
longer than needed is a privacy cost with no benefit. Unfinished rows (waiting for replay, or for
an operator's decision) are never deleted. Deletes run in small batches so no long lock is held
on tables the phone path writes to.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import unscoped
from wassup_core.logging import get_logger

log = get_logger(__name__)

BATCH = 1000
MAX_BATCHES_PER_RUN = 50

_RULES = {
    # finished: processed, or quarantined (a configuration problem, investigated from the alert)
    "retell_events_raw": text(
        """
        DELETE FROM retell_events_raw WHERE id IN (
          SELECT id FROM retell_events_raw
          WHERE received_at < now() - make_interval(days => :days)
            AND (processed_at IS NOT NULL OR error LIKE 'quarantined:%')
          LIMIT :n)
        """
    ),
    "tool_requests_raw": text(
        """
        DELETE FROM tool_requests_raw WHERE id IN (
          SELECT id FROM tool_requests_raw
          WHERE received_at < now() - make_interval(days => :days) AND completed_at IS NOT NULL
          LIMIT :n)
        """
    ),
    "quarantine_events": text(
        """
        DELETE FROM quarantine_events WHERE id IN (
          SELECT id FROM quarantine_events
          WHERE received_at < now() - make_interval(days => :days)
          LIMIT :n)
        """
    ),
}


async def run(engine: AsyncEngine, retention: timedelta) -> dict[str, int]:
    """One pass. Returns rows deleted per table (counts only are logged)."""
    days = max(1, retention.days)
    deleted: dict[str, int] = {}
    for table, statement in _RULES.items():
        total = 0
        for _ in range(MAX_BATCHES_PER_RUN):
            async with unscoped(engine) as conn:  # one short transaction per batch
                count = (await conn.execute(statement, {"days": days, "n": BATCH})).rowcount
            total += count
            if count < BATCH:
                break
        deleted[table] = total
        if total:
            log.info("retention_deleted", job=table, count=total)
    return deleted
