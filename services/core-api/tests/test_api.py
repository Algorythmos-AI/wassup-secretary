"""Staff API against the migrated database, with the service connected as app_core."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from core_api.main import build_app
from core_api.settings import CoreApiSettings
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from wassup_core.db import make_engine
from wassup_core.settings import Environment

from tests.support.database import Seed, as_role

pytestmark = pytest.mark.db

RECEPTIONIST_A = "test:uid-reception-a:reception@a.example.test"
VIEWER_A = "test:uid-viewer-a:viewer@a.example.test"
ADMIN_AB = "test:uid-admin-ab:admin@ab.example.test"
STRANGER = "test:uid-nobody:nobody@example.test"


@pytest.fixture(scope="module")
def staff(db_engine: Engine, seed: Seed) -> None:
    with db_engine.connect() as conn, conn.begin():
        for uid, email, memberships in (
            ("uid-reception-a", "reception@a.example.test", [(seed.clinic_a, "receptionist")]),
            ("uid-viewer-a", "viewer@a.example.test", [(seed.clinic_a, "viewer")]),
            (
                "uid-admin-ab",
                "admin@ab.example.test",
                [(seed.clinic_a, "admin"), (seed.clinic_b, "admin")],
            ),
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
                        "INSERT INTO clinic_memberships (clinic_id, staff_user_id, role) VALUES (:c, :s, :r) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"c": clinic, "s": staff_id, "r": role},
                )


@pytest.fixture
async def client(db_url: str, staff: None) -> AsyncIterator[httpx.AsyncClient]:
    engine = make_engine(db_url, pool_size=2)

    @event.listens_for(engine.sync_engine, "connect")
    def _as_app_core(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("SET ROLE app_core")
        cursor.close()

    settings = CoreApiSettings(environment=Environment.TEST, auth_mode="test")
    transport = httpx.ASGITransport(app=build_app(settings, engine=engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _new_calls(db_engine: Engine, clinic: uuid.UUID, count: int) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in range(count)]
    base = datetime(2026, 9, 1, tzinfo=UTC)
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "wassup_owner", [clinic])
        for i, call_id in enumerate(ids):
            conn.execute(
                text(
                    "INSERT INTO calls (id, clinic_id, provider_call_id, direction, started_at) "
                    "VALUES (:id, :c, :p, 'inbound', :s)"
                ),
                {
                    "id": call_id,
                    "c": clinic,
                    "p": f"call_{call_id.hex}",
                    "s": base + timedelta(minutes=i),
                },
            )
    return ids


async def test_requires_a_valid_sign_in(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/me")).status_code == 401
    assert (await client.get("/v1/me", headers=_auth("garbage"))).status_code == 401
    assert (await client.get("/v1/me", headers=_auth(STRANGER))).status_code == 403


async def test_me_lists_only_own_clinics(client: httpx.AsyncClient, seed: Seed) -> None:
    single = (await client.get("/v1/me", headers=_auth(RECEPTIONIST_A))).json()
    assert [(c["id"], c["role"]) for c in single["clinics"]] == [
        (str(seed.clinic_a), "receptionist")
    ]
    both = (await client.get("/v1/me", headers=_auth(ADMIN_AB))).json()
    assert {c["id"] for c in both["clinics"]} == {str(seed.clinic_a), str(seed.clinic_b)}


async def test_other_clinics_calls_are_not_found(client: httpx.AsyncClient, seed: Seed) -> None:
    response = await client.get(f"/v1/clinics/{seed.clinic_b}/calls", headers=_auth(RECEPTIONIST_A))
    assert response.status_code == 404  # never reveals that the clinic exists
    detail = await client.get(
        f"/v1/clinics/{seed.clinic_a}/calls/{seed.call_b}", headers=_auth(ADMIN_AB)
    )
    assert detail.status_code == 404  # right clinic in the URL, but the call belongs to clinic B


async def test_cursor_pagination_is_complete_and_stable(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    created = set(_new_calls(db_engine, seed.clinic_b, 5))
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (
            await client.get(
                f"/v1/clinics/{seed.clinic_b}/calls", params=params, headers=_auth(ADMIN_AB)
            )
        ).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen))  # no duplicates across pages
    assert {str(c) for c in created} <= set(seen)  # nothing skipped
    assert str(seed.call_b) in seen  # the seed call has no start time: sorted last, still reached


async def test_call_detail_is_audited(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    response = await client.get(
        f"/v1/clinics/{seed.clinic_a}/calls/{seed.call_a}", headers=_auth(RECEPTIONIST_A)
    )
    assert response.status_code == 200
    with db_engine.connect() as conn:
        audited = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'call.view' AND target_id = :t"),
            {"t": str(seed.call_a)},
        ).scalar()
    assert audited and audited >= 1


async def test_workflow_update_is_idempotent_and_optimistically_locked(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    [call_id] = _new_calls(db_engine, seed.clinic_a, 1)
    url = f"/v1/clinics/{seed.clinic_a}/calls/{call_id}/workflow"
    headers = {
        **_auth(RECEPTIONIST_A),
        "Idempotency-Key": "key-" + uuid.uuid4().hex,
        "If-Match": "1",
    }
    first = await client.post(
        url, json={"status": "addressed", "note": "Called back."}, headers=headers
    )
    assert first.status_code == 200 and first.json()["version"] == 2
    retry = await client.post(
        url, json={"status": "addressed", "note": "Called back."}, headers=headers
    )
    assert retry.json() == first.json()  # same key → same answer, no second change
    stale = await client.post(
        url,
        json={"status": "following_up"},
        headers={**headers, "Idempotency-Key": "key-" + uuid.uuid4().hex, "If-Match": "1"},
    )
    assert stale.status_code == 412  # someone else already changed it
    with db_engine.connect() as conn:
        interactions = conn.execute(
            text("SELECT count(*) FROM call_interactions WHERE call_id = :id"), {"id": call_id}
        ).scalar()
    assert interactions == 1


async def test_viewer_cannot_change_workflow(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    [call_id] = _new_calls(db_engine, seed.clinic_a, 1)
    response = await client.post(
        f"/v1/clinics/{seed.clinic_a}/calls/{call_id}/workflow",
        json={"status": "addressed"},
        headers={**_auth(VIEWER_A), "Idempotency-Key": "key-" + uuid.uuid4().hex, "If-Match": "1"},
    )
    assert response.status_code == 403


async def test_workflow_requires_idempotency_and_version_headers(
    client: httpx.AsyncClient, seed: Seed
) -> None:
    response = await client.post(
        f"/v1/clinics/{seed.clinic_a}/calls/{seed.call_a}/workflow",
        json={"status": "addressed"},
        headers=_auth(RECEPTIONIST_A),
    )
    assert response.status_code == 422
