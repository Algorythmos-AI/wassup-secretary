"""Who is calling, and which clinics may they act on."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import unscoped

from core_api.auth import AuthError, TokenVerifier

ROLE_RANK = {"viewer": 0, "receptionist": 1, "admin": 2, "owner": 3}


@dataclass(frozen=True)
class Staff:
    uid: str
    email: str
    staff_user_id: uuid.UUID
    roles: dict[uuid.UUID, str] = field(default_factory=dict)
    expires_at: float | None = None

    def require(self, clinic_id: uuid.UUID, min_role: str = "viewer") -> None:
        """404 when not a member (never reveal that another clinic's data exists); 403 when the
        membership exists but the role is too low."""
        role = self.roles.get(clinic_id)
        if role is None:
            raise HTTPException(status_code=404, detail="Not found")
        if ROLE_RANK[role] < ROLE_RANK[min_role]:
            raise HTTPException(status_code=403, detail="Insufficient role")

    def has_role(self, clinic_id: uuid.UUID, min_role: str) -> bool:
        role = self.roles.get(clinic_id)
        return role is not None and ROLE_RANK[role] >= ROLE_RANK[min_role]


async def _memberships(engine: AsyncEngine, uid: str) -> list[Any]:
    async with unscoped(engine) as conn:
        return list(
            (await conn.execute(text("SELECT * FROM staff_memberships(:uid)"), {"uid": uid}))
            .mappings()
            .all()
        )


async def current_staff(request: Request) -> Staff:
    verifier: TokenVerifier | None = request.app.state.verifier
    engine: AsyncEngine = request.app.state.engine
    if verifier is None:
        raise HTTPException(status_code=503, detail="Authentication is not configured")
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Sign-in required")
    try:
        principal = await run_in_threadpool(verifier.verify, token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in") from exc
    rows = await _memberships(engine, principal.uid)
    if not rows:
        # A newcomer: accept any invitations for this verified email, then look again.
        from core_api.team import enrol  # noqa: PLC0415 — team imports Staff; avoid a cycle

        if await enrol(engine, principal):
            rows = await _memberships(engine, principal.uid)
    if not rows:
        raise HTTPException(status_code=403, detail="No clinic access")
    return Staff(
        uid=principal.uid,
        email=principal.email,
        staff_user_id=rows[0]["staff_user_id"],
        roles={r["clinic_id"]: r["role"] for r in rows},
        expires_at=principal.expires_at,
    )
