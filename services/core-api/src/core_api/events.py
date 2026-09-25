"""Live dashboard events: GET /v1/clinics/{clinic_id}/events (Server-Sent Events).

A stream carries one clinic's outbox events — ``call.analyzed``, ``message.captured``,
``message.urgent``, ``call.workflow`` — as ids only; the screen then fetches what it needs through
the ordinary (audited) endpoints. Guarantees:

- **Only this clinic, only while allowed.** The clinic is checked when the stream opens, every
  query runs under row-level security for that clinic alone, and membership is re-checked every
  ``MEMBERSHIP_RECHECK_S``: a removed staff member's stream ends with ``event: revoked``.
- **Never outlives the sign-in.** The stream ends with ``event: reauth`` when the viewer's token
  expires (or after ``events_max_seconds``); the client reconnects with a fresh token.
- **Gap-free and resumable.** Outbox ids are assigned at insert but become visible at commit, and
  commits land out of order. Events are therefore delivered in (writing transaction, id) order,
  and only from transactions older than every transaction still running, so no event can later
  appear behind the stream's cursor. The SSE ``id`` is that cursor (``<xact>-<id>``); a reconnect
  with ``Last-Event-ID`` continues from it, and one that fell too far behind gets
  ``event: reset`` (reload the screen) instead of a flood. A long-running write transaction
  delays events (never loses them); app roles have a 30 s idle-in-transaction limit.
- **Bounded.** At most ``events_max_per_user`` streams per person and ``events_max_streams`` per
  replica (429 beyond). No database connection is held between polls, a poll interrupted by a
  disconnect still finishes cleanly, and a keep-alive comment keeps proxies from closing idle
  streams.

Browsers' ``EventSource`` can't send an Authorization header, so the web client uses a fetch-based
SSE reader; the token never appears in a URL (and so never in a log).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
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

Cursor = tuple[int, int]  # (writing transaction id, outbox id)

_XMIN = text("SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint")
_AFTER = text(
    """
    SELECT xact_id, id, event_type, payload, created_at FROM outbox_events
    WHERE clinic_id = :c AND (xact_id, id) > (:cx, :cid) AND xact_id < :xmin
    ORDER BY xact_id, id LIMIT :n
    """
)
_BACKLOG = text(
    """
    SELECT count(*) FROM (
      SELECT 1 FROM outbox_events WHERE clinic_id = :c AND (xact_id, id) > (:cx, :cid) LIMIT :cap
    ) backlog
    """
)
_MEMBER = text("SELECT 1 FROM staff_memberships(:uid) WHERE clinic_id = :c")
_CURSOR = re.compile(r"([0-9]{1,18})-([0-9]{1,18})")


@dataclass(eq=False)
class StreamSlot:
    """One open stream. Held only by the stream's generator, so a stream that never started (or
    has ended) drops out of the registry on its own."""

    uid: str
    active: bool = True


def open_slot(
    registry: weakref.WeakSet[StreamSlot], uid: str, per_user: int, total: int
) -> StreamSlot | None:
    live = [slot for slot in registry if slot.active]
    if len(live) >= total or sum(1 for slot in live if slot.uid == uid) >= per_user:
        return None
    slot = StreamSlot(uid)
    registry.add(slot)
    return slot


def _frame(event: str, data: dict[str, Any], cursor: Cursor | None = None) -> str:
    head = f"id: {cursor[0]}-{cursor[1]}\n" if cursor is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _resume_point(last_event_id: str | None) -> Cursor | None:
    match = _CURSOR.fullmatch(last_event_id or "")
    return (int(match[1]), int(match[2])) if match else None


async def _start(
    engine: AsyncEngine, clinic_id: uuid.UUID, resume: Cursor | None
) -> tuple[Cursor, bool]:
    """Where to start, and whether the client must reload (too far behind to replay). A fresh
    stream starts at "now": everything already committed is excluded, and everything from
    transactions still running (which the screen could not have loaded yet) is included."""
    async with clinic_scope(engine, [clinic_id]) as conn:
        now_cursor = (int((await conn.execute(_XMIN)).scalar_one()), 0)
        if resume is None or resume > now_cursor:
            return now_cursor, False
        backlog = (
            await conn.execute(
                _BACKLOG,
                {"c": clinic_id, "cx": resume[0], "cid": resume[1], "cap": MAX_BACKLOG + 1},
            )
        ).scalar_one()
    if backlog > MAX_BACKLOG:
        return now_cursor, True
    return resume, False


async def _fetch_after(engine: AsyncEngine, clinic_id: uuid.UUID, cursor: Cursor) -> Sequence[Any]:
    async with clinic_scope(engine, [clinic_id]) as conn:
        xmin = int((await conn.execute(_XMIN)).scalar_one())
        return (
            await conn.execute(
                _AFTER,
                {"c": clinic_id, "cx": cursor[0], "cid": cursor[1], "xmin": xmin, "n": BATCH},
            )
        ).all()


async def _still_member(engine: AsyncEngine, uid: str, clinic_id: uuid.UUID) -> bool:
    async with unscoped(engine) as conn:
        return (await conn.execute(_MEMBER, {"uid": uid, "c": clinic_id})).first() is not None


async def _stream(
    request: Request,
    engine: AsyncEngine,
    staff: Staff,
    clinic_id: uuid.UUID,
    resume: Cursor | None,
    *,
    slot: StreamSlot | None = None,
) -> AsyncIterator[str]:
    settings: CoreApiSettings = request.app.state.settings
    try:
        now = time.time()
        deadline = now + settings.events_max_seconds
        if staff.expires_at is not None:
            deadline = min(deadline, staff.expires_at)
        cursor, reset = await _start(engine, clinic_id, resume)
        yield "retry: 3000\n\n"
        # Every stream opens with its start position as an event id, so a client that
        # reconnects before any real event arrives still resumes exactly where it was (without
        # it, a reconnect would restart at "now" and skip whatever was committed in between).
        # Clients reload their lists on it: nothing committed before it can be missed.
        if reset:
            yield _frame("reset", {"reason": "too_far_behind"}, cursor)
        else:
            yield _frame("ready", {}, cursor)
        next_check = now + MEMBERSHIP_RECHECK_S
        next_keepalive = now + KEEPALIVE_S
        while time.time() < deadline:
            if await request.is_disconnected():
                return
            # Shielded: a disconnect mid-query lets the poll finish and return its connection
            # cleanly instead of abandoning it half-way through a transaction.
            rows = await asyncio.shield(_fetch_after(engine, clinic_id, cursor))
            for row in rows:
                cursor = (int(row.xact_id), int(row.id))
                data = {**(row.payload or {}), "at": row.created_at.isoformat()}
                yield _frame(row.event_type, data, cursor)
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
    finally:
        if slot is not None:
            slot.active = False


@router.get("/clinics/{clinic_id}/events")
async def clinic_events(
    clinic_id: uuid.UUID,
    staff: StaffDep,
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    staff.require(clinic_id)
    state = request.app.state
    settings: CoreApiSettings = state.settings
    slot = open_slot(
        state.event_streams, staff.uid, settings.events_max_per_user, settings.events_max_streams
    )
    if slot is None:
        raise HTTPException(status_code=429, detail="Too many live streams")
    engine: AsyncEngine = state.engine
    return StreamingResponse(
        _stream(request, engine, staff, clinic_id, _resume_point(last_event_id), slot=slot),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
