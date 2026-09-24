"""Live dashboard events: GET /v1/clinics/{clinic_id}/events (Server-Sent Events).

A stream carries one clinic's outbox events — ``call.analyzed``, ``message.captured``,
``message.urgent``, ``call.workflow`` — as ids only; the screen then fetches what it needs through
the ordinary (audited) endpoints. Guarantees:

- **Only this clinic, only while allowed.** The clinic is checked when the stream opens, every
  query runs under row-level security for that clinic alone, and membership is re-checked every
  ``MEMBERSHIP_RECHECK_S``: a removed staff member's stream ends with ``event: revoked``.
- **Never outlives the sign-in.** The stream ends with ``event: reauth`` when the viewer's token
  expires (or after ``events_max_seconds``); the client reconnects with a fresh token.
- **Resumable.** Each event's ``id`` is its outbox id; a reconnect with ``Last-Event-ID`` continues
  from there. A reconnect that fell too far behind gets ``event: reset`` (reload the screen)
  instead of a flood.
- **Cheap.** No database connection is held between polls; a keep-alive comment keeps proxies
  from closing an idle stream.

Browsers' ``EventSource`` can't send an Authorization header, so the web client uses a fetch-based
SSE reader; the token never appears in a URL (and so never in a log).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import clinic_scope, unscoped

from core_api.settings import CoreApiSettings
from core_api.staff import Staff, current_staff

router = APIRouter(prefix="/v1")
StaffDep = Annotated[Staff, Depends(current_staff)]

BATCH = 100
MAX_BACKLOG = 1000
MEMBERSHIP_RECHECK_S = 300.0
KEEPALIVE_S = 15.0

_LATEST = text("SELECT coalesce(max(id), 0) FROM outbox_events WHERE clinic_id = :c")
_AFTER = text(
    """
    SELECT id, event_type, payload, created_at FROM outbox_events
    WHERE clinic_id = :c AND id > :after ORDER BY id LIMIT :n
    """
)
_BACKLOG = text(
    """
    SELECT count(*) FROM (
      SELECT 1 FROM outbox_events WHERE clinic_id = :c AND id > :after LIMIT :cap
    ) backlog
    """
)
_MEMBER = text("SELECT 1 FROM staff_memberships(:uid) WHERE clinic_id = :c")


def _frame(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _resume_point(last_event_id: str | None) -> int | None:
    if last_event_id is None or not last_event_id.isascii() or not last_event_id.isdigit():
        return None
    return int(last_event_id[:18])


async def _start(engine: AsyncEngine, clinic_id: uuid.UUID, resume: int | None) -> tuple[int, bool]:
    """Where to start, and whether the client must reload (too far behind to replay)."""
    async with clinic_scope(engine, [clinic_id]) as conn:
        latest = int((await conn.execute(_LATEST, {"c": clinic_id})).scalar_one())
        if resume is None or resume > latest:
            return latest, False
        backlog = (
            await conn.execute(_BACKLOG, {"c": clinic_id, "after": resume, "cap": MAX_BACKLOG + 1})
        ).scalar_one()
    if backlog > MAX_BACKLOG:
        return latest, True
    return resume, False


async def _still_member(engine: AsyncEngine, uid: str, clinic_id: uuid.UUID) -> bool:
    async with unscoped(engine) as conn:
        return (await conn.execute(_MEMBER, {"uid": uid, "c": clinic_id})).first() is not None


async def _stream(
    request: Request, engine: AsyncEngine, staff: Staff, clinic_id: uuid.UUID, resume: int | None
) -> AsyncIterator[str]:
    settings: CoreApiSettings = request.app.state.settings
    now = time.time()
    deadline = now + settings.events_max_seconds
    if staff.expires_at is not None:
        deadline = min(deadline, staff.expires_at)
    after, reset = await _start(engine, clinic_id, resume)
    yield "retry: 3000\n\n"
    if reset:
        yield _frame("reset", {"reason": "too_far_behind"}, after)
    next_check = now + MEMBERSHIP_RECHECK_S
    next_keepalive = now + KEEPALIVE_S
    while time.time() < deadline:
        if await request.is_disconnected():
            return
        async with clinic_scope(engine, [clinic_id]) as conn:
            rows = (await conn.execute(_AFTER, {"c": clinic_id, "after": after, "n": BATCH})).all()
        for row in rows:
            after = int(row.id)
            data = {**(row.payload or {}), "at": row.created_at.isoformat()}
            yield _frame(row.event_type, data, after)
        now = time.time()
        if now >= next_check:
            if not await _still_member(engine, staff.uid, clinic_id):
                yield _frame("revoked", {})
                return
            next_check = now + MEMBERSHIP_RECHECK_S
        if now >= next_keepalive:
            yield ": keep-alive\n\n"
            next_keepalive = now + KEEPALIVE_S
        if len(rows) < BATCH:  # caught up: wait; otherwise keep draining
            await asyncio.sleep(settings.events_poll_interval_s)
    yield _frame("reauth", {})


@router.get("/clinics/{clinic_id}/events")
async def clinic_events(
    clinic_id: uuid.UUID,
    staff: StaffDep,
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    staff.require(clinic_id)
    engine: AsyncEngine = request.app.state.engine
    return StreamingResponse(
        _stream(request, engine, staff, clinic_id, _resume_point(last_event_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
