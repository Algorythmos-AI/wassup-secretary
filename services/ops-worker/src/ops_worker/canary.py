"""Daily end-to-end line check ("canary") for every AI phone line.

Why: in September 2026 the telephony account was suspended for 13 days and nothing noticed. The
voice provider saw zero calls — not even failed ones — so no error-based monitor could fire, and
low-volume clinics routinely go days without a call. The only reliable detector is to ring each
line and confirm the call arrives.

How: once per local day at ``canary_local_time`` each AI line is called FROM another AI line
with a one-line "line check" agent, through the real path. voice-gateway recognises the call as
synthetic and records its receipt in ``canary_runs``. A run that Retell refuses, or that is not
received within ``RECEIPT_WINDOW``, alerts ops once by email. ``status`` also fails when no run happened in
``STALE_AFTER`` — a dead scheduler is itself an outage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import unscoped
from wassup_core.logging import get_logger

from ops_worker.notifier import EmailSender
from ops_worker.retell_api import RetellApi

log = get_logger(__name__)

RECEIPT_WINDOW = timedelta(minutes=15)
STALE_AFTER = timedelta(hours=26)


@dataclass(frozen=True)
class CanaryConfig:
    enabled: bool
    agent_id: str
    agent_version: int | None
    local_time: str
    timezone: str
    lines: list[str]
    ops_emails: list[str]


def routes(lines: list[str]) -> list[tuple[str, str]]:
    """(to, from): each line is called from the next one; a single line has nothing to call from."""
    if len(lines) < 2:
        return []
    return [(to, lines[(i + 1) % len(lines)]) for i, to in enumerate(lines)]


def local_date_if_due(cfg: CanaryConfig, now: datetime) -> str | None:
    """The local calendar date if the daily run is due at ``now``, else None. DST-aware."""
    local = now.astimezone(ZoneInfo(cfg.timezone))
    hour, minute = (int(p) for p in cfg.local_time.split(":"))
    if (local.hour, local.minute) < (hour, minute):
        return None
    return local.date().isoformat()


async def _alert(sender: EmailSender, cfg: CanaryConfig, line: str, reason: str) -> None:
    if not cfg.ops_emails:
        log.error("canary_failed_nobody_alerted", reason=reason)
        return
    body = "\n".join(
        [
            f"The daily automated line check for {line} failed: {reason}.",
            "",
            "Real patient calls to this line are probably not reaching the AI receptionist.",
            "Check in order: voice provider call log -> telephony account status and billing ->",
            "SIP trunk -> the clinic phone system divert.",
            "Runbook: docs/runbooks/phone-line-down.md",
        ]
    )
    try:
        await sender.send(cfg.ops_emails, f"WASSUP line check FAILED ({line})", body)
    except Exception as exc:  # alerting must never crash the job; the health endpoint still fails
        log.error("canary_alert_failed", code=type(exc).__name__)


async def tick(
    engine: AsyncEngine, retell: RetellApi, sender: EmailSender, cfg: CanaryConfig, now: datetime
) -> dict[str, int]:
    """One scheduler tick; safe to run every minute from any number of workers."""
    if not cfg.enabled or not cfg.agent_id:
        return {"placed": 0, "failed": 0}
    placed = failed = 0
    run_date = local_date_if_due(cfg, now)
    if run_date is not None:
        for to_number, from_number in routes(cfg.lines):
            async with unscoped(engine) as conn:
                claimed = (
                    await conn.execute(
                        text(
                            """
                            INSERT INTO canary_runs (run_date, to_number, from_number, status)
                            VALUES (:d, :to, :from, 'placing')
                            ON CONFLICT (run_date, to_number) DO NOTHING RETURNING id
                            """
                        ),
                        {
                            "d": datetime.fromisoformat(run_date).date(),
                            "to": to_number,
                            "from": from_number,
                        },
                    )
                ).scalar()
            if claimed is None:
                continue
            try:
                call_id = await retell.create_phone_call(
                    from_number, to_number, cfg.agent_id, cfg.agent_version
                )
                status, error = "placed", None
                placed += 1
            except Exception as exc:
                call_id, status, error = None, "failed", f"place_failed:{type(exc).__name__}"
                failed += 1
            async with unscoped(engine) as conn:
                await conn.execute(
                    text(
                        "UPDATE canary_runs SET status = :s, placed_at = :now, "
                        "provider_call_id = :c, error = :e WHERE id = :id"
                    ),
                    {"s": status, "now": now, "c": call_id, "e": error, "id": claimed},
                )
            if error:
                log.error("canary_place_failed", reason=error)
                await _alert(
                    sender, cfg, to_number, "the voice provider could not place the test call"
                )

    async with unscoped(engine) as conn:
        overdue = (
            (
                await conn.execute(
                    text(
                        """
                    UPDATE canary_runs SET status = 'failed', error = 'not_received'
                    WHERE status = 'placed' AND received_at IS NULL AND placed_at < :cutoff
                    RETURNING to_number
                    """
                    ),
                    {"cutoff": now - RECEIPT_WINDOW},
                )
            )
            .scalars()
            .all()
        )
    for line in overdue:
        failed += 1
        log.error("canary_not_received", reason="not_received")
        await _alert(
            sender, cfg, line, "the test call was placed but never reached the AI receptionist"
        )
    return {"placed": placed, "failed": failed}


def assess(latest: list[dict[str, Any]], cfg: CanaryConfig, now: datetime) -> dict[str, Any]:
    """Health verdict over the latest run per line."""
    if not cfg.enabled:
        return {"status": "disabled", "lines": []}
    by_line = {r["to_number"]: r for r in latest}
    lines = []
    for line in cfg.lines:
        run = by_line.get(line)
        if run is None or run["placed_at"] is None or now - run["placed_at"] > STALE_AFTER:
            state = "stale"
        elif run["status"] in ("received", "late"):
            state = "ok"
        elif run["status"] == "placed" and now - run["placed_at"] < RECEIPT_WINDOW:
            state = "pending"
        else:
            state = "failing"
        lines.append(
            {"line": line, "state": state, "run_date": str(run["run_date"]) if run else None}
        )
    failing = any(line["state"] in ("failing", "stale") for line in lines)
    return {"status": "failing" if failing else "ok", "lines": lines}


async def status(engine: AsyncEngine, cfg: CanaryConfig, now: datetime) -> dict[str, Any]:
    if not cfg.enabled:
        return assess([], cfg, now)
    async with unscoped(engine) as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        """
                    SELECT DISTINCT ON (to_number) to_number, run_date, status, placed_at
                    FROM canary_runs ORDER BY to_number, run_date DESC, id DESC
                    """
                    )
                )
            )
            .mappings()
            .all()
        )
    return assess([dict(r) for r in rows], cfg, now)
