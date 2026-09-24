"""Transactional-outbox consumer (ADR 0005).

- Claims due events with ``FOR UPDATE SKIP LOCKED`` so any number of workers can run.
- A claim is a 5-minute lease (``available_at`` moves forward); an event whose worker died is
  picked up again when its lease expires.
- Each event is handled in its own clinic-scoped transaction. Failure → back to ``pending`` with
  exponential backoff; after ``MAX_ATTEMPTS`` it is dead-lettered (``dead``) and logged loudly.
- Alert delivery is recorded per (event, channel) as ``sending`` → ``sent``. A retry skips
  anything already sent, so a crash can cause at most a duplicate email, never a lost one.
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
        raise RuntimeError("message_not_found")
    recipients = _alert_recipients(clinic["alert_contacts"])
    if not recipients:
        raise RuntimeError("no_alert_contacts")

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
}


async def _claim(engine: AsyncEngine, batch_size: int) -> list[OutboxEvent]:
    async with unscoped(engine) as conn:
        clinics = (await conn.execute(text("SELECT active_clinic_ids()"))).scalar() or []
    if not clinics:
        return []
    async with clinic_scope(engine, clinics) as conn:
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
                      ORDER BY id
                      FOR UPDATE SKIP LOCKED
                      LIMIT :n
                    )
                    RETURNING id, clinic_id, event_type, dedupe_key, payload, attempts
                    """
                    ),
                    {"n": batch_size, "lease": LEASE.total_seconds()},
                )
            )
            .mappings()
            .all()
        )
    return [OutboxEvent(**dict(r)) for r in rows]


async def _finish(engine: AsyncEngine, event: OutboxEvent, error: str | None) -> None:
    async with clinic_scope(engine, [event.clinic_id]) as conn:
        if error is None:
            await conn.execute(
                text(
                    "UPDATE outbox_events SET status = 'done', processed_at = now(), last_error = NULL WHERE id = :id"
                ),
                {"id": event.id},
            )
            return
        dead = event.attempts >= MAX_ATTEMPTS
        backoff_s = min(3600, 30 * 2 ** (event.attempts - 1))
        await conn.execute(
            text(
                """
                UPDATE outbox_events
                SET status = :status, last_error = :error,
                    available_at = now() + make_interval(secs => :backoff)
                WHERE id = :id
                """
            ),
            {
                "status": "dead" if dead else "pending",
                "error": error[:200],
                "backoff": backoff_s,
                "id": event.id,
            },
        )
        if dead:
            log.error(
                "outbox_dead_letter", event_id=event.id, event_type=event.event_type, reason=error
            )


async def process_batch(
    engine: AsyncEngine, email: EmailSender, *, batch_size: int = 20, dashboard_url: str = ""
) -> int:
    """Claim and handle one batch. Returns how many events were claimed."""
    events = await _claim(engine, batch_size)
    for event in events:
        handler = HANDLERS.get(event.event_type)
        error: str | None = None
        if handler is None:
            error = "no_handler"
        else:
            try:
                async with clinic_scope(engine, [event.clinic_id]) as conn:
                    await handler(HandlerContext(conn, email, dashboard_url), event)
            except Exception as exc:
                error = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        if error:
            log.warning(
                "outbox_event_failed",
                event_type=event.event_type,
                attempt=event.attempts,
                reason=error,
            )
        await _finish(engine, event, error)
    return len(events)
