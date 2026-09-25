"""Who works at a clinic: invitations, roles, removal (admins and owners).

Nothing here needs a privileged writer. An admin's changes run as ``app_core`` under the clinic's
row-level security; a newcomer's membership is created at their own sign-in, under the invited
clinic's scope, from an invitation the resolver role found by email. Every change is audited.

Invariants, enforced on every call:
- you can only give a role no higher than your own, and only an owner can make an owner;
- you can only change or remove someone whose role is no higher than your own;
- a clinic always keeps at least one owner;
- access ends at once for the API (memberships are read per request) and within one poll for
  live streams (see events.py).
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from wassup_core.db import clinic_scope, unscoped
from wassup_core.logging import get_logger

from core_api.auth import Principal
from core_api.schemas import InviteRequest, RoleChange, Team, TeamChange
from core_api.staff import ROLE_RANK, Staff, current_staff

router = APIRouter(prefix="/v1")
StaffDep = Annotated[Staff, Depends(current_staff)]
log = get_logger(__name__)

_MEMBERS = text("SELECT * FROM clinic_members(:c)")
_INVITATIONS = text(
    "SELECT id, email::text AS email, role, created_at FROM clinic_invitations "
    "WHERE clinic_id = :c AND accepted_at IS NULL AND revoked_at IS NULL ORDER BY created_at"
)
_OWNERS = text("SELECT count(*) FROM clinic_memberships WHERE clinic_id = :c AND role = 'owner'")
_ROLE_OF = text("SELECT role FROM clinic_memberships WHERE clinic_id = :c AND staff_user_id = :s")


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


async def _audit(
    conn: AsyncConnection,
    clinic_id: uuid.UUID,
    actor: uuid.UUID,
    action: str,
    *,
    target_type: str,
    target_id: str,
    detail: dict[str, Any],
) -> None:
    await conn.execute(
        text(
            "INSERT INTO audit_log "
            "(clinic_id, actor_staff_user_id, action, target_type, target_id, detail) "
            "VALUES (:c, :a, :action, :t, :id, CAST(:d AS jsonb))"
        ),
        {
            "c": clinic_id,
            "a": actor,
            "action": action,
            "t": target_type,
            "id": target_id,
            "d": json.dumps(detail),
        },
    )


async def _team(conn: AsyncConnection, clinic_id: uuid.UUID) -> dict[str, Any]:
    members = (await conn.execute(_MEMBERS, {"c": clinic_id})).mappings().all()
    invitations = (await conn.execute(_INVITATIONS, {"c": clinic_id})).mappings().all()
    return {"members": [dict(m) for m in members], "invitations": [dict(i) for i in invitations]}


def _check_can_grant(staff: Staff, clinic_id: uuid.UUID, role: str) -> None:
    own = staff.roles[clinic_id]
    if ROLE_RANK[role] > ROLE_RANK[own] or (role == "owner" and own != "owner"):
        raise HTTPException(status_code=403, detail="You can't give a role above your own")


async def _check_can_change(
    conn: AsyncConnection, staff: Staff, clinic_id: uuid.UUID, target: uuid.UUID
) -> str:
    """The target's current role, after checking the actor outranks or equals it. Changes to a
    clinic's memberships are serialised per clinic first, so the owner count below is exact."""
    if target == staff.staff_user_id:
        raise HTTPException(
            status_code=409, detail="You can't change your own access; ask another admin"
        )
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('team:' || CAST(:c AS text)))"),
        {"c": str(clinic_id)},
    )
    current = (await conn.execute(_ROLE_OF, {"c": clinic_id, "s": target})).scalar()
    if current is None:
        raise HTTPException(status_code=404, detail="Not a member")
    if ROLE_RANK[current] > ROLE_RANK[staff.roles[clinic_id]]:
        raise HTTPException(status_code=403, detail="You can't change someone above your role")
    return str(current)


async def _check_keeps_an_owner(
    conn: AsyncConnection, clinic_id: uuid.UUID, current_role: str, new_role: str | None
) -> None:
    if current_role == "owner" and new_role != "owner":
        owners = int((await conn.execute(_OWNERS, {"c": clinic_id})).scalar_one())
        if owners <= 1:
            raise HTTPException(status_code=409, detail="A clinic must keep at least one owner")


# --- enrolment ----------------------------------------------------------------------------------


_ACCEPT = text(
    """
    WITH inviter AS (
      SELECT i.id AS invitation_id, i.clinic_id, i.role, m.role AS inviter_role
      FROM clinic_invitations i
      LEFT JOIN clinic_memberships m
        ON m.clinic_id = i.clinic_id AND m.staff_user_id = i.invited_by
      WHERE i.id = :id AND i.accepted_at IS NULL AND i.revoked_at IS NULL
    ),
    accepted AS (
      UPDATE clinic_invitations SET accepted_at = now(), accepted_by = :s
      WHERE id = (SELECT invitation_id FROM inviter
                  WHERE CASE inviter_role WHEN 'owner' THEN 3 WHEN 'admin' THEN 2
                                          WHEN 'receptionist' THEN 1 WHEN 'viewer' THEN 0 END
                        >= CASE role WHEN 'owner' THEN 3 WHEN 'admin' THEN 2
                                     WHEN 'receptionist' THEN 1 WHEN 'viewer' THEN 0 END)
      RETURNING clinic_id, role
    ),
    membership AS (
      INSERT INTO clinic_memberships (clinic_id, staff_user_id, role)
      SELECT clinic_id, :s, role FROM accepted
      ON CONFLICT (clinic_id, staff_user_id) DO NOTHING
    )
    SELECT role FROM accepted
    """
)
_LAPSE = text(
    "UPDATE clinic_invitations SET revoked_at = now() "
    "WHERE id = :id AND accepted_at IS NULL AND revoked_at IS NULL"
)


async def enrol(engine: AsyncEngine, principal: Principal) -> int:
    """Accept any open invitations for a verified sign-in's email. Only then is a staff row
    created (a stranger who signs in leaves nothing behind). Each acceptance is one statement:
    the invitation is closed and the membership created together, so an invitation revoked a
    moment earlier grants nothing. An invitation whose inviter no longer holds a rank at or
    above the invited role lapses instead. Returns how many memberships were created."""
    async with unscoped(engine) as conn:
        invited = (
            (await conn.execute(text("SELECT * FROM invited_clinics(:e)"), {"e": principal.email}))
            .mappings()
            .all()
        )
    if not invited:
        return 0
    async with unscoped(engine) as conn:
        staff_user_id = (
            await conn.execute(
                text("SELECT enrol_staff(:uid, :email, NULL)"),
                {"uid": principal.uid, "email": principal.email},
            )
        ).scalar_one()
    accepted = 0
    for invitation in invited:
        clinic_id = uuid.UUID(str(invitation["clinic_id"]))
        invitation_id = invitation["invitation_id"]
        async with clinic_scope(engine, [clinic_id]) as conn:
            role = (await conn.execute(_ACCEPT, {"id": invitation_id, "s": staff_user_id})).scalar()
            if role is None:
                # Revoked meanwhile, already accepted, or the inviter can no longer vouch for
                # that role: it lapses, visibly, rather than granting anything.
                lapsed = (await conn.execute(_LAPSE, {"id": invitation_id})).rowcount
                if lapsed:
                    await _audit(
                        conn,
                        clinic_id,
                        staff_user_id,
                        "invitation.lapsed",
                        target_type="invitation",
                        target_id=str(invitation_id),
                        detail={"reason": "inviter_rank"},
                    )
                continue
            await _audit(
                conn,
                clinic_id,
                staff_user_id,
                "membership.accepted",
                target_type="membership",
                target_id=str(staff_user_id),
                detail={"role": role, "invitation_id": str(invitation_id)},
            )
        accepted += 1
    if accepted:
        log.info("invitations_accepted", count=accepted)
    return accepted


# --- routes ------------------------------------------------------------------------------------


@router.get("/clinics/{clinic_id}/team", response_model=Team)
async def team(clinic_id: uuid.UUID, staff: StaffDep, request: Request) -> dict[str, Any]:
    staff.require(clinic_id, "admin")
    async with clinic_scope(_engine(request), [clinic_id]) as conn:
        return await _team(conn, clinic_id)


@router.post("/clinics/{clinic_id}/team/invitations", response_model=TeamChange, status_code=201)
async def invite(
    clinic_id: uuid.UUID, body: InviteRequest, staff: StaffDep, request: Request
) -> dict[str, Any]:
    staff.require(clinic_id, "admin")
    _check_can_grant(staff, clinic_id, body.role)
    email = body.email.strip()  # case is the database's business (citext), never Python's
    async with clinic_scope(_engine(request), [clinic_id]) as conn:
        member = await conn.execute(
            text("SELECT 1 FROM clinic_members(:c) WHERE email::citext = CAST(:e AS citext)"),
            {"c": clinic_id, "e": email},
        )
        if member.first() is not None:
            raise HTTPException(status_code=409, detail="Already a member of this clinic")
        try:
            invitation_id = (
                await conn.execute(
                    text(
                        "INSERT INTO clinic_invitations (clinic_id, email, role, invited_by) "
                        "VALUES (:c, :e, :r, :by) RETURNING id"
                    ),
                    {"c": clinic_id, "e": email, "r": body.role, "by": staff.staff_user_id},
                )
            ).scalar_one()
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Already invited") from exc
        await _audit(
            conn,
            clinic_id,
            staff.staff_user_id,
            "invitation.created",
            target_type="invitation",
            target_id=str(invitation_id),
            detail={"role": body.role},
        )
        return {"action": "invited", "team": await _team(conn, clinic_id)}


@router.delete("/clinics/{clinic_id}/team/invitations/{invitation_id}", response_model=TeamChange)
async def revoke_invitation(
    clinic_id: uuid.UUID, invitation_id: uuid.UUID, staff: StaffDep, request: Request
) -> dict[str, Any]:
    staff.require(clinic_id, "admin")
    async with clinic_scope(_engine(request), [clinic_id]) as conn:
        revoked = (
            await conn.execute(
                text(
                    "UPDATE clinic_invitations SET revoked_at = now() WHERE id = :id "
                    "AND clinic_id = :c AND accepted_at IS NULL AND revoked_at IS NULL"
                ),
                {"id": invitation_id, "c": clinic_id},
            )
        ).rowcount
        if not revoked:
            raise HTTPException(status_code=404, detail="No such open invitation")
        await _audit(
            conn,
            clinic_id,
            staff.staff_user_id,
            "invitation.revoked",
            target_type="invitation",
            target_id=str(invitation_id),
            detail={},
        )
        return {"action": "invitation_revoked", "team": await _team(conn, clinic_id)}


@router.patch("/clinics/{clinic_id}/team/members/{staff_user_id}", response_model=TeamChange)
async def change_role(
    clinic_id: uuid.UUID,
    staff_user_id: uuid.UUID,
    body: RoleChange,
    staff: StaffDep,
    request: Request,
) -> dict[str, Any]:
    staff.require(clinic_id, "admin")
    _check_can_grant(staff, clinic_id, body.role)
    async with clinic_scope(_engine(request), [clinic_id]) as conn:
        current = await _check_can_change(conn, staff, clinic_id, staff_user_id)
        await _check_keeps_an_owner(conn, clinic_id, current, body.role)
        await conn.execute(
            text(
                "UPDATE clinic_memberships SET role = :r "
                "WHERE clinic_id = :c AND staff_user_id = :s"
            ),
            {"r": body.role, "c": clinic_id, "s": staff_user_id},
        )
        await _audit(
            conn,
            clinic_id,
            staff.staff_user_id,
            "membership.role_changed",
            target_type="membership",
            target_id=str(staff_user_id),
            detail={"from": current, "to": body.role},
        )
        return {"action": "role_changed", "team": await _team(conn, clinic_id)}


@router.delete("/clinics/{clinic_id}/team/members/{staff_user_id}", response_model=TeamChange)
async def remove_member(
    clinic_id: uuid.UUID, staff_user_id: uuid.UUID, staff: StaffDep, request: Request
) -> dict[str, Any]:
    staff.require(clinic_id, "admin")
    async with clinic_scope(_engine(request), [clinic_id]) as conn:
        current = await _check_can_change(conn, staff, clinic_id, staff_user_id)
        await _check_keeps_an_owner(conn, clinic_id, current, None)
        await conn.execute(
            text("DELETE FROM clinic_memberships WHERE clinic_id = :c AND staff_user_id = :s"),
            {"c": clinic_id, "s": staff_user_id},
        )
        await _audit(
            conn,
            clinic_id,
            staff.staff_user_id,
            "membership.removed",
            target_type="membership",
            target_id=str(staff_user_id),
            detail={"role": current},
        )
        return {"action": "removed", "team": await _team(conn, clinic_id)}
