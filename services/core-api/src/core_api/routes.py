"""Staff API v1: the signed-in user, calls, call detail and workflow actions."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from wassup_core.db import clinic_scope

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
    try:
        started, call_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return (datetime.fromisoformat(started) if started else None), uuid.UUID(call_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc


async def _audit(
    conn: AsyncConnection, clinic_id: uuid.UUID, staff: Staff, action: str, target_id: str
) -> None:
    await conn.execute(
        text(
            "INSERT INTO audit_log (clinic_id, actor_staff_user_id, action, target_type, target_id) "
            "VALUES (:c, :actor, :action, 'call', :target)"
        ),
        {"c": clinic_id, "actor": staff.staff_user_id, "action": action, "target": target_id},
    )


@router.get("/me")
async def me(staff: StaffDep, request: Request) -> dict[str, Any]:
    async with clinic_scope(_engine(request), list(staff.roles)) as conn:
        clinics = (
            (await conn.execute(text("SELECT id, slug, name, timezone FROM clinics ORDER BY name")))
            .mappings()
            .all()
        )
    return {
        "email": staff.email,
        "clinics": [
            {**{k: str(v) for k, v in c.items()}, "role": staff.roles[c["id"]]} for c in clinics
        ],
    }


_LIST_SELECT = (
    "SELECT id, started_at, from_number, duration_seconds, summary, intent, workflow_status, "
    "is_priority, is_reception_action, version FROM calls WHERE clinic_id = :c "
)
_LIST_ORDER = " ORDER BY started_at DESC NULLS LAST, id DESC LIMIT :limit"
# Keyset pagination on (started_at DESC NULLS LAST, id DESC) — stable under concurrent inserts.
# Three fixed statements (no SQL assembled at runtime). Rows without a start time sort last, and a
# comparison with NULL is never true, so they are admitted explicitly once dated rows run out.
_FIRST_PAGE = text(_LIST_SELECT + _LIST_ORDER)
_AFTER_DATED = text(
    _LIST_SELECT
    + "AND ((started_at, id) < (:started, :last_id) OR started_at IS NULL)"
    + _LIST_ORDER
)
_AFTER_UNDATED = text(_LIST_SELECT + "AND started_at IS NULL AND id < :last_id" + _LIST_ORDER)


@router.get("/clinics/{clinic_id}/calls")
async def list_calls(
    clinic_id: uuid.UUID,
    staff: StaffDep,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    staff.require(clinic_id)
    params: dict[str, Any] = {"c": clinic_id, "limit": limit + 1}
    statement = _FIRST_PAGE
    if cursor:
        started, last_id = _decode_cursor(cursor)
        params.update({"started": started, "last_id": last_id})
        statement = _AFTER_DATED if started else _AFTER_UNDATED
    async with clinic_scope(_engine(request), [clinic_id]) as conn:
        rows = (await conn.execute(statement, params)).mappings().all()
    page = [dict(r) for r in rows[:limit]]
    next_cursor = (
        _encode_cursor(page[-1]["started_at"], page[-1]["id"]) if len(rows) > limit else None
    )
    return {"items": page, "next_cursor": next_cursor}


@router.get("/clinics/{clinic_id}/calls/{call_id}")
async def call_detail(
    clinic_id: uuid.UUID, call_id: uuid.UUID, staff: StaffDep, request: Request
) -> dict[str, Any]:
    staff.require(clinic_id)
    async with clinic_scope(_engine(request), [clinic_id]) as conn:
        call = (
            (await conn.execute(text("SELECT * FROM calls WHERE id = :id"), {"id": call_id}))
            .mappings()
            .first()
        )
        if call is None:
            raise HTTPException(status_code=404, detail="Not found")
        messages = (
            (
                await conn.execute(
                    text(
                        "SELECT category, detail, callback_number, created_at FROM messages WHERE provider_call_id = :p ORDER BY created_at"
                    ),
                    {"p": call["provider_call_id"]},
                )
            )
            .mappings()
            .all()
        )
        interactions = (
            (
                await conn.execute(
                    text(
                        "SELECT action_type, status_from, status_to, note, created_at FROM call_interactions WHERE call_id = :id ORDER BY created_at"
                    ),
                    {"id": call_id},
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


@router.post("/clinics/{clinic_id}/calls/{call_id}/workflow")
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
            await conn.execute(
                text(
                    "SELECT 1 FROM call_interactions WHERE clinic_id = :c AND idempotency_key = :k"
                ),
                {"c": clinic_id, "k": idempotency_key},
            )
        ).first()
        if replay is not None:
            return {
                "call_id": str(call_id),
                "workflow_status": current["workflow_status"],
                "version": current["version"],
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
        await conn.execute(
            text(
                "INSERT INTO call_interactions (clinic_id, call_id, action_type, status_from, status_to, note, "
                "actor_staff_user_id, idempotency_key) VALUES (:c, :id, 'status_change', :from, :to, :note, :actor, :k)"
            ),
            {
                "c": clinic_id,
                "id": call_id,
                "from": current["workflow_status"],
                "to": action.status,
                "note": action.note,
                "actor": staff.staff_user_id,
                "k": idempotency_key,
            },
        )
        await _audit(conn, clinic_id, staff, "call.workflow", str(call_id))
    return {"call_id": str(call_id), "workflow_status": action.status, "version": version}
