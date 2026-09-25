"""Team management: invitations, enrolment at sign-in, role changes, removal, and the invariants."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from core_api.main import build_app
from core_api.settings import CoreApiSettings
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import make_engine
from wassup_core.settings import Environment

from tests.support.database import Seed

pytestmark = pytest.mark.db

OWNER_A = "test:uid-owner-a:owner@a.example.test"
ADMIN_A = "test:uid-admin-a:admin@a.example.test"
RECEPTION_A = "test:uid-reception-a2:reception2@a.example.test"
ADMIN_B = "test:uid-admin-b:admin@b.example.test"
NEWCOMER = "test:uid-new-1:New.Person@example.test"  # mixed case: emails compare case-insensitively


@pytest.fixture(scope="module")
def team_seed(db_engine: Engine, seed: Seed) -> None:
    with db_engine.connect() as conn, conn.begin():
        for uid, email, memberships in (
            ("uid-owner-a", "owner@a.example.test", [(seed.clinic_a, "owner")]),
            ("uid-admin-a", "admin@a.example.test", [(seed.clinic_a, "admin")]),
            ("uid-reception-a2", "reception2@a.example.test", [(seed.clinic_a, "receptionist")]),
            ("uid-admin-b", "admin@b.example.test", [(seed.clinic_b, "admin")]),
        ):
            staff_id = conn.execute(
                text(
                    "INSERT INTO staff_users (firebase_uid, email) VALUES (:u, :e) "
                    "ON CONFLICT (firebase_uid) DO UPDATE SET email = EXCLUDED.email RETURNING id"
                ),
                {"u": uid, "e": email},
            ).scalar_one()
            for clinic, role in memberships:
                conn.execute(
                    text(
                        "INSERT INTO clinic_memberships (clinic_id, staff_user_id, role) "
                        "VALUES (:c, :s, :r) ON CONFLICT (clinic_id, staff_user_id) "
                        "DO UPDATE SET role = EXCLUDED.role"
                    ),
                    {"c": clinic, "s": staff_id, "r": role},
                )


@pytest.fixture
async def client(db_url: str, team_seed: None) -> AsyncIterator[httpx.AsyncClient]:
    engine: AsyncEngine = make_engine(db_url, pool_size=2)

    @event.listens_for(engine.sync_engine, "connect")
    def _as_app_core(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("SET ROLE app_core")
        cursor.close()

    settings = CoreApiSettings(environment=Environment.TEST, auth_mode="test")
    app = build_app(settings, engine=engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    await engine.dispose()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rows(db_engine: Engine, sql: str, **params: Any) -> list[Any]:
    with db_engine.connect() as conn:
        return list(conn.execute(text(sql), params).mappings())


async def _team(client: httpx.AsyncClient, clinic: uuid.UUID, who: str) -> dict[str, Any]:
    response = await client.get(f"/v1/clinics/{clinic}/team", headers=_auth(who))
    assert response.status_code == 200, response.text
    return response.json()


async def test_only_admins_and_owners_see_the_team(client: httpx.AsyncClient, seed: Seed) -> None:
    assert (
        await client.get(f"/v1/clinics/{seed.clinic_a}/team", headers=_auth(RECEPTION_A))
    ).status_code == 403
    assert (
        await client.get(f"/v1/clinics/{seed.clinic_a}/team", headers=_auth(ADMIN_B))
    ).status_code == 404
    team = await _team(client, seed.clinic_a, ADMIN_A)
    emails = {m["email"] for m in team["members"]}
    assert {"owner@a.example.test", "admin@a.example.test", "reception2@a.example.test"} <= emails
    assert "admin@b.example.test" not in emails


async def test_invite_then_the_newcomer_is_enrolled_at_first_sign_in(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    # Before the invitation, the newcomer has no access at all.
    assert (await client.get("/v1/me", headers=_auth(NEWCOMER))).status_code == 403

    response = await client.post(
        f"/v1/clinics/{seed.clinic_a}/team/invitations",
        json={"email": "new.person@example.test", "role": "receptionist"},
        headers=_auth(ADMIN_A),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["action"] == "invited"
    assert [i["email"] for i in body["team"]["invitations"]] == ["new.person@example.test"]

    # Same email, different case, at sign-in: the membership is created and the invitation closed.
    me = await client.get("/v1/me", headers=_auth(NEWCOMER))
    assert me.status_code == 200, me.text
    assert [(c["id"], c["role"]) for c in me.json()["clinics"]] == [
        (str(seed.clinic_a), "receptionist")
    ]
    team = await _team(client, seed.clinic_a, ADMIN_A)
    assert team["invitations"] == []
    assert any(m["email"].lower() == "new.person@example.test" for m in team["members"])
    [accepted] = _rows(
        db_engine,
        "SELECT accepted_at, accepted_by FROM clinic_invitations WHERE email = 'new.person@example.test'",
    )
    assert accepted["accepted_at"] is not None and accepted["accepted_by"] is not None
    actions = [
        r["action"]
        for r in _rows(
            db_engine,
            "SELECT action FROM audit_log WHERE clinic_id = :c AND action LIKE 'invitation.%' "
            "OR action LIKE 'membership.%' ORDER BY id",
            c=seed.clinic_a,
        )
    ]
    assert "invitation.created" in actions and "membership.accepted" in actions

    # Inviting a member again is refused; a second open invitation for one email too.
    dup = await client.post(
        f"/v1/clinics/{seed.clinic_a}/team/invitations",
        json={"email": "NEW.PERSON@example.test", "role": "viewer"},
        headers=_auth(ADMIN_A),
    )
    assert dup.status_code == 409


async def test_nobody_grants_above_their_own_role(client: httpx.AsyncClient, seed: Seed) -> None:
    url = f"/v1/clinics/{seed.clinic_a}/team/invitations"
    assert (
        await client.post(
            url, json={"email": "x@example.test", "role": "owner"}, headers=_auth(ADMIN_A)
        )
    ).status_code == 403
    ok = await client.post(
        url, json={"email": "future-owner@example.test", "role": "owner"}, headers=_auth(OWNER_A)
    )
    assert ok.status_code == 201
    invitation = next(
        i for i in ok.json()["team"]["invitations"] if i["email"] == "future-owner@example.test"
    )
    revoked = await client.delete(f"{url}/{invitation['id']}", headers=_auth(ADMIN_A))
    assert revoked.status_code == 200 and revoked.json()["action"] == "invitation_revoked"
    assert (
        await client.delete(f"{url}/{invitation['id']}", headers=_auth(ADMIN_A))
    ).status_code == 404


async def test_role_changes_and_removal_keep_an_owner_and_respect_rank(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    team = await _team(client, seed.clinic_a, OWNER_A)
    by_email = {m["email"]: m["staff_user_id"] for m in team["members"]}
    owner, admin, reception = (
        by_email["owner@a.example.test"],
        by_email["admin@a.example.test"],
        by_email["reception2@a.example.test"],
    )
    members = f"/v1/clinics/{seed.clinic_a}/team/members"

    # An admin can't touch the owner, can't make owners, and the last owner can't be demoted.
    assert (
        await client.patch(f"{members}/{owner}", json={"role": "viewer"}, headers=_auth(ADMIN_A))
    ).status_code == 403
    assert (
        await client.patch(f"{members}/{reception}", json={"role": "owner"}, headers=_auth(ADMIN_A))
    ).status_code == 403
    assert (
        await client.patch(f"{members}/{owner}", json={"role": "admin"}, headers=_auth(OWNER_A))
    ).status_code == 409
    assert (await client.delete(f"{members}/{owner}", headers=_auth(OWNER_A))).status_code == 409

    # An admin can change and remove those at or below their rank.
    changed = await client.patch(
        f"{members}/{reception}", json={"role": "viewer"}, headers=_auth(ADMIN_A)
    )
    assert changed.status_code == 200 and changed.json()["action"] == "role_changed"
    assert (
        next(m for m in changed.json()["team"]["members"] if m["staff_user_id"] == reception)[
            "role"
        ]
        == "viewer"
    )
    # The demoted person's own access changes at once.
    me = await client.get("/v1/me", headers=_auth(RECEPTION_A))
    assert me.json()["clinics"][0]["role"] == "viewer"

    removed = await client.delete(f"{members}/{reception}", headers=_auth(ADMIN_A))
    assert removed.status_code == 200 and removed.json()["action"] == "removed"
    assert (
        await client.get("/v1/me", headers=_auth(RECEPTION_A))
    ).status_code == 403  # gone at once
    assert (
        await client.delete(f"{members}/{reception}", headers=_auth(ADMIN_A))
    ).status_code == 404

    # A second owner makes demotion of the first possible.
    assert (
        await client.patch(f"{members}/{admin}", json={"role": "owner"}, headers=_auth(OWNER_A))
    ).status_code == 200
    assert (
        await client.patch(f"{members}/{owner}", json={"role": "admin"}, headers=_auth(ADMIN_A))
    ).status_code == 200
    # Restore the fixture for other tests.
    assert (
        await client.patch(f"{members}/{owner}", json={"role": "owner"}, headers=_auth(ADMIN_A))
    ).status_code == 200
    assert (
        await client.patch(f"{members}/{admin}", json={"role": "admin"}, headers=_auth(OWNER_A))
    ).status_code == 200
    audit = _rows(
        db_engine,
        "SELECT count(*) AS n FROM audit_log WHERE clinic_id = :c AND action IN "
        "('membership.role_changed', 'membership.removed')",
        c=seed.clinic_a,
    )
    assert audit[0]["n"] >= 6


async def test_another_clinics_admin_cannot_manage_this_team(
    client: httpx.AsyncClient, seed: Seed
) -> None:
    team = await _team(client, seed.clinic_a, OWNER_A)
    reception = next(
        (m["staff_user_id"] for m in team["members"] if m["role"] == "receptionist"), None
    )
    target = reception or team["members"][0]["staff_user_id"]
    base = f"/v1/clinics/{seed.clinic_a}/team"
    assert (
        await client.post(
            f"{base}/invitations",
            json={"email": "y@example.test", "role": "viewer"},
            headers=_auth(ADMIN_B),
        )
    ).status_code == 404
    assert (
        await client.patch(
            f"{base}/members/{target}", json={"role": "viewer"}, headers=_auth(ADMIN_B)
        )
    ).status_code == 404
    assert (
        await client.delete(f"{base}/members/{target}", headers=_auth(ADMIN_B))
    ).status_code == 404


async def test_invitations_are_scoped_by_clinic_isolation(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    assert (
        await client.post(
            f"/v1/clinics/{seed.clinic_b}/team/invitations",
            json={"email": "only-b@example.test", "role": "viewer"},
            headers=_auth(ADMIN_B),
        )
    ).status_code == 201
    team_a = await _team(client, seed.clinic_a, ADMIN_A)
    assert all(i["email"] != "only-b@example.test" for i in team_a["invitations"])
    # The newcomer enrols into clinic B only.
    me = await client.get("/v1/me", headers=_auth("test:uid-only-b:only-b@example.test"))
    assert [c["id"] for c in me.json()["clinics"]] == [str(seed.clinic_b)]
