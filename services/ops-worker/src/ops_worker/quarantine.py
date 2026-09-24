"""Quarantine monitor: signed provider traffic we could not attribute to a clinic.

voice-gateway quarantines a webhook event or tool request when the signed agent and the dialled
number don't resolve to one clinic (a new agent not yet mapped, a number moved, a URL slug that
doesn't match). A quarantined webhook never becomes a call record, so until someone acts, that
patient's call is invisible on every dashboard. This job emails ops whenever new quarantined items
appear, and ``/health/quarantine`` stays red while any are unresolved (runbook:
docs/runbooks/quarantine.md). Reports carry counts and reason codes only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import unscoped
from wassup_core.logging import get_logger

from ops_worker.notifier import EmailSender
from ops_worker.watch import Watch

log = get_logger(__name__)

STALE_AFTER_S = 3600
REALERT_EVERY_S = 24 * 3600

_OPEN = text(
    """
    SELECT split_part(reason, ':', 1) AS reason, count(*) AS n, min(received_at) AS oldest
    FROM quarantine_events WHERE resolved_at IS NULL GROUP BY 1 ORDER BY 1
    """
)


@dataclass
class QuarantineMonitor:
    ops_emails: list[str]
    watch: Watch = field(default_factory=lambda: Watch(STALE_AFTER_S, REALERT_EVERY_S))
    alerted_count: int = 0

    def report(self, now: float | None = None) -> dict[str, Any]:
        return self.watch.report(now)


async def check(engine: AsyncEngine) -> dict[str, Any]:
    async with unscoped(engine) as conn:
        rows = (await conn.execute(_OPEN)).all()
    unresolved = sum(int(r.n) for r in rows)
    return {
        "status": "failing" if unresolved else "ok",
        "unresolved": unresolved,
        "by_reason": {r.reason: int(r.n) for r in rows},
        "oldest": min(r.oldest for r in rows).isoformat() if rows else None,
    }


async def tick(monitor: QuarantineMonitor, engine: AsyncEngine, email: EmailSender) -> str:
    report = await check(engine)
    alert_due, _recovered = monitor.watch.record(report)
    unresolved = int(report["unresolved"])
    if unresolved > monitor.alerted_count or (alert_due and unresolved):
        await _alert(email, monitor.ops_emails, report)
    monitor.alerted_count = unresolved
    if unresolved:
        log.error("quarantine_unresolved", count=unresolved)
    return str(report["status"])


async def _alert(email: EmailSender, ops_emails: list[str], report: dict[str, Any]) -> None:
    if not ops_emails:
        log.error("quarantine_nobody_alerted", count=report["unresolved"])
        return
    body = "\n".join(
        [
            f"{report['unresolved']} quarantined item(s) are waiting for a decision.",
            "Each is signed provider traffic that couldn't be matched to a clinic; a quarantined",
            "call does not appear on any dashboard until it is resolved.",
            "",
            *(f"{reason}: {n}" for reason, n in report["by_reason"].items()),
            f"Oldest: {report['oldest']}",
            "",
            "Runbook: docs/runbooks/quarantine.md",
        ]
    )
    try:
        await email.send(ops_emails, "WASSUP quarantined calls need a decision", body)
    except Exception as exc:
        log.error("quarantine_alert_failed", code=type(exc).__name__)
