"""Transactional-outbox consumer (ADR 0005).

- Claims due events with ``FOR UPDATE SKIP LOCKED`` so any number of workers can run.
- A claim is a 5-minute lease (``available_at`` moves forward); an event whose worker died is
  picked up again when its lease expires.
- Each event is handled in its own clinic-scoped transaction. Failure → back to ``pending`` with
  exponential backoff; after ``MAX_ATTEMPTS`` it is dead-lettered (``dead``) and logged loudly.
- Alert delivery is recorded per (event, channel) as ``sending`` → ``sent``. A retry skips
  anything already sent, so a crash can cause at most a duplicate email, never a lost one.
- Finishing is fenced on (status, attempts): a worker whose lease expired and whose event was
  re-claimed by another worker can't overwrite the newer attempt's outcome.
- A dead letter emails ops immediately (ids only), and ``/health/outbox`` stays red until an
  operator re-queues or abandons it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from wassup_core.db import clinic_scope, unscoped
from wassup_core.logging import get_logger

from ops_worker.notifier import EmailSender

log = get_logger(__name__)

MAX_ATTEMPTS = 8
LEASE = timedelta(minutes=5)
# About 3.5 minutes of consecutive failures (30 s, 60 s, 120 s backoff).
RETRYING_ALERT_AT = 3


class DeliveryError(RuntimeError):
    """A handler failure whose message is ours (a short code), safe to store and log. Any other
    exception is recorded by type name only: its message could carry personal data."""


@dataclass(frozen=True)
class OutboxEvent:
    id: int
    clinic_id: uuid.UUID
    event_type: str
    dedupe_key: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True)
class HandlerContext:
    conn: AsyncConnection
    email: EmailSender
    dashboard_url: str


Handler = Callable[[HandlerContext, OutboxEvent], Awaitable[None]]


async def _noop(_ctx: HandlerContext, _event: OutboxEvent) -> None:
    """Events consumed elsewhere (e.g. live dashboard updates) need no worker action."""


def _alert_recipients(alert_contacts: Any) -> list[str]:
    emails: list[str] = []
    for contact in alert_contacts or []:
        if isinstance(contact, str) and "@" in contact:
            emails.append(contact)
        elif (
            isinstance(contact, dict)
            and isinstance(contact.get("email"), str)
            and contact.get("urgent", True)
        ):
            emails.append(contact["email"])
    return emails


async def deliver_urgent_message(ctx: HandlerContext, event: OutboxEvent) -> None:
    """Email the clinic's alert contacts about an urgent message captured on a call."""
    message_key = event.dedupe_key.split(":", 1)[1]
    message = (
        (
            await ctx.conn.execute(
                text(
                    "SELECT category, detail, callback_number, created_at FROM messages WHERE dedupe_key = :k"
                ),
                {"k": message_key},
            )
        )
        .mappings()
        .first()
    )
    clinic = (
        (
            await ctx.conn.execute(
                text("SELECT name, alert_contacts FROM clinics WHERE id = :id"),
                {"id": event.clinic_id},
            )
        )
        .mappings()
        .one()
    )
    if message is None:
        raise DeliveryError("message_not_found")
    recipients = _alert_recipients(clinic["alert_contacts"])
    if not recipients:
        raise DeliveryError("no_alert_contacts")

    claimed = (
        await ctx.conn.execute(
            text(
                """
                INSERT INTO notifications_sent (event_id, channel, status) VALUES (:id, 'email', 'sending')
                ON CONFLICT (event_id, channel) DO UPDATE SET status = notifications_sent.status
                RETURNING status
                """
            ),
            {"id": event.id},
        )
    ).scalar_one()
    if claimed == "sent":
        return
    subject = f"URGENT message for {clinic['name']} (WASSUP)"  # no personal data in subjects
    body = "\n".join(
        line
        for line in (
            "An urgent message was taken by the WASSUP AI receptionist.",
            "",
            f"Callback number: {message['callback_number'] or 'not given'}",
            f"Message: {message['detail']}",
            f"Received: {message['created_at']:%Y-%m-%d %H:%M} UTC",
            f"Dashboard: {ctx.dashboard_url}" if ctx.dashboard_url else None,
        )
        if line is not None
    )
    await ctx.email.send(recipients, subject, body)
    await ctx.conn.execute(
        text(
            "UPDATE notifications_sent SET status = 'sent', sent_at = now() WHERE event_id = :id AND channel = 'email'"
        ),
        {"id": event.id},
    )


HANDLERS: dict[str, Handler] = {
    "message.urgent": deliver_urgent_message,
    "message.captured": _noop,
    "call.analyzed": _noop,
    "call.workflow": _noop,  # consumed by core-api's live event streams
}


# A lease that expired on the last allowed attempt means the worker died mid-handler every time
# (a crash loop, e.g. out of memory). Re-claiming it forever would hide that, so it dies here.
_EXPIRE_EXHAUSTED = text(
    """
    UPDATE outbox_events SET status = 'dead', last_error = 'lease_expired_at_max_attempts'
    WHERE status = 'processing' AND available_at <= now() AND attempts >= :max
    RETURNING id, clinic_id, event_type, dedupe_key, payload, attempts
    """
)


async def _active_clinics(engine: AsyncEngine) -> list[uuid.UUID]:
    async with unscoped(engine) as conn:
        return list((await conn.execute(text("SELECT active_clinic_ids()"))).scalar() or [])


async def _claim(
    engine: AsyncEngine, batch_size: int
) -> tuple[list[OutboxEvent], list[OutboxEvent]]:
    """Returns (claimed events, events just dead-lettered for exhausting their leases)."""
    clinics = await _active_clinics(engine)
    if not clinics:
        return [], []
    async with clinic_scope(engine, clinics) as conn:
        exhausted = (await conn.execute(_EXPIRE_EXHAUSTED, {"max": MAX_ATTEMPTS})).mappings().all()
        rows = (
            (
                await conn.execute(
                    text(
                        """
                    UPDATE outbox_events SET status = 'processing', attempts = attempts + 1,
                                             available_at = now() + make_interval(secs => :lease)
                    WHERE id IN (
                      SELECT id FROM outbox_events
                      WHERE status IN ('pending', 'processing') AND available_at <= now()
                        AND attempts < :max
                      ORDER BY id
                      FOR UPDATE SKIP LOCKED
                      LIMIT :n
                    )
                    RETURNING id, clinic_id, event_type, dedupe_key, payload, attempts
                    """
                    ),
                    {"n": batch_size, "lease": LEASE.total_seconds(), "max": MAX_ATTEMPTS},
                )
            )
            .mappings()
            .all()
        )
    return [OutboxEvent(**dict(r)) for r in rows], [OutboxEvent(**dict(r)) for r in exhausted]


# Only the attempt that holds the current lease may record an outcome (fenced on status + attempts).
_DONE = text(
    "UPDATE outbox_events SET status = 'done', processed_at = now(), last_error = NULL "
    "WHERE id = :id AND status = 'processing' AND attempts = :attempts"
)
_FAILED = text(
    "UPDATE outbox_events SET status = :status, last_error = :error, "
    "available_at = now() + make_interval(secs => :backoff) "
    "WHERE id = :id AND status = 'processing' AND attempts = :attempts"
)


async def _finish(engine: AsyncEngine, event: OutboxEvent, error: str | None) -> bool:
    """Record the outcome. Returns whether the event is now dead-lettered."""
    fence = {"id": event.id, "attempts": event.attempts}
    dead = error is not None and event.attempts >= MAX_ATTEMPTS
    async with clinic_scope(engine, [event.clinic_id]) as conn:
        if error is None:
            result = await conn.execute(_DONE, fence)
        else:
            result = await conn.execute(
                _FAILED,
                {
                    **fence,
                    "status": "dead" if dead else "pending",
                    "error": error[:200],
                    "backoff": min(3600, 30 * 2 ** (event.attempts - 1)),
                },
            )
    if result.rowcount == 0:
        # Our lease expired and another worker re-claimed the event; its outcome stands.
        log.warning("outbox_stale_finish", event_id=event.id, attempt=event.attempts)
        return False
    if dead:
        log.error(
            "outbox_dead_letter", event_id=event.id, event_type=event.event_type, reason=error
        )
    return dead


async def _alert_dead_letter(
    email: EmailSender, ops_emails: list[str], event: OutboxEvent, error: str
) -> None:
    if not ops_emails:
        log.error("outbox_dead_letter_nobody_alerted", event_id=event.id)
        return
    body = "\n".join(
        (
            f"Outbox event {event.id} ({event.event_type}) failed {event.attempts} times and was "
            "dead-lettered. It will not be retried until someone decides.",
            f"Clinic id: {event.clinic_id}",
            f"Last error: {error}",
            "",
            "Runbook: docs/runbooks/outbox-dead-letter.md (re-queue or abandon).",
        )
    )
    try:
        await email.send(ops_emails, f"WASSUP outbox dead letter ({event.event_type})", body)
    except Exception as exc:
        log.error("outbox_dead_letter_alert_failed", event_id=event.id, code=type(exc).__name__)


async def process_batch(
    engine: AsyncEngine,
    email: EmailSender,
    *,
    batch_size: int = 20,
    dashboard_url: str = "",
    ops_emails: list[str] | None = None,
) -> int:
    """Claim and handle one batch. Returns how many events were claimed."""
    events, exhausted = await _claim(engine, batch_size)
    for event in exhausted:
        log.error("outbox_dead_letter", event_id=event.id, event_type=event.event_type)
        await _alert_dead_letter(email, ops_emails or [], event, "lease_expired_at_max_attempts")
    for event in events:
        handler = HANDLERS.get(event.event_type)
        error: str | None = None
        if handler is None:
            error = "no_handler"
        else:
            try:
                async with clinic_scope(engine, [event.clinic_id]) as conn:
                    await handler(HandlerContext(conn, email, dashboard_url), event)
            except DeliveryError as exc:
                error = str(exc)
            except Exception as exc:
                error = type(exc).__name__
        if error:
            log.warning(
                "outbox_event_failed",
                event_type=event.event_type,
                attempt=event.attempts,
                reason=error,
            )
        if await _finish(engine, event, error) and error is not None:
            await _alert_dead_letter(email, ops_emails or [], event, error)
    return len(events)


async def health(engine: AsyncEngine, max_pending_age: timedelta) -> dict[str, Any]:
    """Counts only. 'failing' when anything is dead-lettered, an event has waited too long (a
    stuck or crashed consumer), or an event keeps failing (an urgent alert that can't be
    delivered shouldn't wait for all its retries to be exhausted before someone hears about it)."""
    clinics = await _active_clinics(engine)
    async with clinic_scope(engine, clinics) as conn:
        row = (
            (
                await conn.execute(
                    text(
                        """
                        SELECT count(*) FILTER (WHERE status = 'dead') AS dead,
                               count(*) FILTER (WHERE status IN ('pending', 'processing')
                                                  AND created_at < now() - make_interval(secs => :age)
                                                  AND available_at < now()) AS overdue,
                               count(*) FILTER (WHERE status IN ('pending', 'processing')
                                                  AND attempts >= :retrying) AS retrying,
                               count(*) FILTER (WHERE status IN ('pending', 'processing')) AS open
                        FROM outbox_events
                        WHERE status IN ('pending', 'processing', 'dead')
                        """
                    ),
                    {"age": max_pending_age.total_seconds(), "retrying": RETRYING_ALERT_AT},
                )
            )
            .mappings()
            .one()
        )
    counts = {key: int(value) for key, value in row.items()}
    failing = counts["dead"] or counts["overdue"] or counts["retrying"]
    return {**counts, "status": "failing" if failing else "ok"}
