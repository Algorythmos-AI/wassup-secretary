"""Staff API v1: the signed-in user, calls, call detail and workflow actions."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from wassup_core.db import clinic_scope

from core_api.schemas import CallDetail, CallPage, Me, WorkflowResult
from core_api.staff import Staff, current_staff

router = APIRouter(prefix="/v1")
StaffDep = Annotated[Staff, Depends(current_staff)]


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


def _encode_cursor(started_at: datetime | None, call_id: uuid.UUID) -> str:
    raw = json.dumps([started_at.isoformat() if started_at else None, str(call_id)])
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime | None, uuid.UUID]:
    """Cursors come from clients, so every part is checked: anything unexpected is a 400."""
    invalid = HTTPException(status_code=400, detail="Invalid cursor")
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (ValueError, UnicodeError) as exc:
        raise invalid from exc
    if not (isinstance(value, list) and len(value) == 2):
        raise invalid
    started, call_id = value
    if not isinstance(call_id, str) or not (started is None or isinstance(started, str)):
        raise invalid
    try:
        started_at = datetime.fromisoformat(started) if started is not None else None
        parsed_id = uuid.UUID(call_id)
    except ValueError as exc:
        raise invalid from exc
    if started_at is not None and started_at.tzinfo is None:
        raise invalid
    return started_at, parsed_id


async def _audit(
    conn: AsyncConnection,
    clinic_id: uuid.UUID,
    staff: Staff,
    action: str,
    target_id: str | None,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write an audit row in the caller's transaction (so a read and its audit commit together).
    The database chains each row to the clinic's previous one (see migration 0004)."""
    await conn.execute(
        text(
            "INSERT INTO audit_log (clinic_id, actor_staff_user_id, action, target_type, target_id, detail) "
            "VALUES (:c, :actor, :action, 'call', :target, CAST(:detail AS jsonb))"
        ),
        {
            "c": clinic_id,
            "actor": staff.staff_user_id,
            "action": action,
            "target": target_id,
            "detail": json.dumps(detail) if detail is not None else None,
        },
    )


@router.get("/me", response_model=Me)
async def me(staff: StaffDep, request: Request) -> dict[str, Any]:
    async with clinic_scope(_engine(request), list(staff.roles)) as conn:
        clinics = (
            (await conn.execute(text("SELECT id, slug, name, timezone FROM clinics ORDER BY name")))
            .mappings()
            .all()
        )
    return {
        "email": staff.email,
        "clinics": [{**dict(c), "role": staff.roles[c["id"]]} for c in clinics],
    }


# has_urgent_message: did the caller leave a message the voice agent flagged urgent? (Served by
# the partial index from migration 0009; written out in each statement to keep them literal.)
_LIST_SELECT = (
    "SELECT id, started_at, from_number, duration_seconds, summary, intent, workflow_status, "
    "is_priority, is_reception_action, version, "
    "EXISTS (SELECT 1 FROM messages m WHERE m.clinic_id = calls.clinic_id "
    "AND m.provider_call_id = calls.provider_call_id AND m.urgent) AS has_urgent_message "
    "FROM calls WHERE clinic_id = :c "
)
_OPEN_ONLY = "AND workflow_status IN ('pending', 'following_up') "
_LIST_ORDER = " ORDER BY started_at DESC NULLS LAST, id DESC LIMIT :limit"
_AFTER_DATED_SQL = "AND ((started_at, id) < (:started, :last_id) OR started_at IS NULL)"
_AFTER_UNDATED_SQL = "AND started_at IS NULL AND id < :last_id"
# Keyset pagination on (started_at DESC NULLS LAST, id DESC) — stable under concurrent inserts.
# Fixed statements (no SQL assembled per request). Rows without a start time sort last, and a
# comparison with NULL is never true, so they are admitted explicitly once dated rows run out.
# Keyed by (open_only, cursor kind).
_LIST = {
    (open_only, kind): text(
        _LIST_SELECT
        + (_OPEN_ONLY if open_only else "")
        + {"first": "", "dated": _AFTER_DATED_SQL, "undated": _AFTER_UNDATED_SQL}[kind]
        + _LIST_ORDER
    )
    for open_only in (False, True)
    for kind in ("first", "dated", "undated")
}


@router.get("/clinics/{clinic_id}/calls", response_model=CallPage)
async def list_calls(
    clinic_id: uuid.UUID,
    staff: StaffDep,
    request: Request,
    *,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    open_only: Annotated[
        bool, Query(description="Only calls still to do (pending or following up)")
    ] = False,
) -> dict[str, Any]:
    staff.require(clinic_id)
    params: dict[str, Any] = {"c": clinic_id, "limit": limit + 1}
    kind = "first"
    if cursor:
        started, last_id = _decode_cursor(cursor)
        params.update({"started": started, "last_id": last_id})
        kind = "dated" if started else "undated"
    statement = _LIST[(open_only, kind)]
    async with clinic_scope(_engine(request), [clinic_id]) as conn:
        rows = (await conn.execute(statement, params)).mappings().all()
        page = [dict(r) for r in rows[:limit]]
        # The list carries caller numbers and summaries, so it is audited like a detail view:
        # which calls this person was shown, and when.
        await _audit(
            conn,
            clinic_id,
            staff,
            "call.list",
            None,
            detail={"call_ids": [str(r["id"]) for r in page]},
        )
    next_cursor = (
        _encode_cursor(page[-1]["started_at"], page[-1]["id"]) if len(rows) > limit else None
    )
    return {"items": page, "next_cursor": next_cursor}


_DETAIL = text(
    """
    SELECT id, provider_call_id, direction, from_number, to_number, started_at, ended_at,
           duration_seconds, cost_usd::text AS cost_usd, disconnection_reason, summary, transcript,
           sentiment, intent, is_priority, is_reception_action, local_date, local_hour,
           workflow_status, version, analyzed_at,
           EXISTS (SELECT 1 FROM messages m WHERE m.clinic_id = calls.clinic_id
                   AND m.provider_call_id = calls.provider_call_id AND m.urgent)
             AS has_urgent_message
    FROM calls WHERE id = :id AND clinic_id = :c
    """
)
_MESSAGES = text(
    "SELECT category, detail, callback_number, urgent, created_at FROM messages "
    "WHERE clinic_id = :c AND provider_call_id = :p ORDER BY created_at"
)


@router.get("/clinics/{clinic_id}/calls/{call_id}", response_model=CallDetail)
async def call_detail(
    clinic_id: uuid.UUID, call_id: uuid.UUID, staff: StaffDep, request: Request
) -> dict[str, Any]:
    staff.require(clinic_id)
    async with clinic_scope(_engine(request), [clinic_id]) as conn:
        call = (await conn.execute(_DETAIL, {"id": call_id, "c": clinic_id})).mappings().first()
        if call is None:
            raise HTTPException(status_code=404, detail="Not found")
        messages = (
            (await conn.execute(_MESSAGES, {"c": clinic_id, "p": call["provider_call_id"]}))
            .mappings()
            .all()
        )
        interactions = (
            (
                await conn.execute(
                    text(
                        "SELECT action_type, status_from, status_to, note, created_at FROM call_interactions "
                        "WHERE clinic_id = :c AND call_id = :id ORDER BY created_at"
                    ),
                    {"id": call_id, "c": clinic_id},
                )
            )
            .mappings()
            .all()
        )
        await _audit(conn, clinic_id, staff, "call.view", str(call_id))  # every PII read is audited
    return {
        "call": dict(call),
        "messages": [dict(m) for m in messages],
        "interactions": [dict(i) for i in interactions],
    }


class WorkflowAction(BaseModel):
    status: Literal["pending", "following_up", "addressed", "no_action_needed"]
    note: str | None = Field(default=None, max_length=2000)


def _request_hash(call_id: uuid.UUID, if_match: int, action: WorkflowAction) -> str:
    canonical = json.dumps(
        {"call_id": str(call_id), "if_match": if_match, **action.model_dump()}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _key_reused() -> HTTPException:
    return HTTPException(
        status_code=422, detail="Idempotency-Key was already used for a different request"
    )


@router.post("/clinics/{clinic_id}/calls/{call_id}/workflow", response_model=WorkflowResult)
async def update_workflow(
    clinic_id: uuid.UUID,
    call_id: uuid.UUID,
    *,
    action: WorkflowAction,
    staff: StaffDep,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=100)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> dict[str, Any]:
    """Change a call's workflow status. ``If-Match`` carries the version the user saw (two
    receptionists can't silently overwrite each other); ``Idempotency-Key`` makes retries safe."""
    staff.require(clinic_id, "receptionist")
    request_hash = _request_hash(call_id, if_match, action)
    async with clinic_scope(_engine(request), [clinic_id]) as conn:
        current = (
            (
                await conn.execute(
                    text("SELECT workflow_status, version FROM calls WHERE id = :id FOR UPDATE"),
                    {"id": call_id},
                )
            )
            .mappings()
            .first()
        )
        if current is None:
            raise HTTPException(status_code=404, detail="Not found")
        replay = (
            (
                await conn.execute(
                    text(
                        "SELECT call_id, request_hash, status_to, result_version FROM call_interactions "
                        "WHERE clinic_id = :c AND idempotency_key = :k"
                    ),
                    {"c": clinic_id, "k": idempotency_key},
                )
            )
            .mappings()
            .first()
        )
        if replay is not None:
            # The same key only ever means the same request: then the original answer is returned
            # (even if the call has changed since). Anything else is a client bug, not a retry.
            if replay["call_id"] != call_id or replay["request_hash"] != request_hash:
                raise _key_reused()
            return {
                "call_id": str(call_id),
                "workflow_status": replay["status_to"],
                "version": replay["result_version"],
            }
        if current["version"] != if_match:
            raise HTTPException(
                status_code=412, detail=f"Call changed (current version {current['version']})"
            )
        version = (
            await conn.execute(
                text(
                    "UPDATE calls SET workflow_status = :s, version = version + 1, updated_at = now() "
                    "WHERE id = :id RETURNING version"
                ),
                {"s": action.status, "id": call_id},
            )
        ).scalar_one()
        try:
            await conn.execute(
                text(
                    "INSERT INTO call_interactions (clinic_id, call_id, action_type, status_from, status_to, note, "
                    "actor_staff_user_id, idempotency_key, request_hash, result_version) "
                    "VALUES (:c, :id, 'status_change', :from, :to, :note, :actor, :k, :h, :v)"
                ),
                {
                    "c": clinic_id,
                    "id": call_id,
                    "from": current["workflow_status"],
                    "to": action.status,
                    "note": action.note,
                    "actor": staff.staff_user_id,
                    "k": idempotency_key,
                    "h": request_hash,
                    "v": version,
                },
            )
        except IntegrityError as exc:
            if "idempotency_key" not in str(exc.orig):
                raise
            # The same key raced in on another call; this transaction (and its update) rolls back.
            raise _key_reused() from exc
        await _audit(conn, clinic_id, staff, "call.workflow", str(call_id))
        # Every other screen of this clinic hears about the change (ids only).
        await conn.execute(
            text(
                "INSERT INTO outbox_events (clinic_id, event_type, dedupe_key, payload) "
                "VALUES (:c, 'call.workflow', :k, CAST(:p AS jsonb)) ON CONFLICT (dedupe_key) DO NOTHING"
            ),
            {
                "c": clinic_id,
                "k": f"call.workflow:{call_id}:{version}",
                "p": json.dumps({"call_id": str(call_id), "version": version}),
            },
        )
    return {"call_id": str(call_id), "workflow_status": action.status, "version": version}
