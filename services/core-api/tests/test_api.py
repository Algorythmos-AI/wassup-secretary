"""Staff API against the migrated database, with the service connected as app_core."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from core_api.main import build_app
from core_api.schemas import CallRecord
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
    with db_engine.connect() as conn:
        live = (
            conn.execute(
                text(
                    "SELECT payload FROM outbox_events WHERE event_type = 'call.workflow' AND payload->>'call_id' = :id"
                ),
                {"id": str(call_id)},
            )
            .scalars()
            .all()
        )
    assert live == [{"call_id": str(call_id), "version": 2}]  # other screens hear about it once


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


async def test_idempotency_key_is_bound_to_one_request(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    first_call, second_call = _new_calls(db_engine, seed.clinic_a, 2)
    key = "key-" + uuid.uuid4().hex

    def post(call_id: uuid.UUID, status: str, version: str = "1") -> Any:
        return client.post(
            f"/v1/clinics/{seed.clinic_a}/calls/{call_id}/workflow",
            json={"status": status},
            headers={**_auth(RECEPTIONIST_A), "Idempotency-Key": key, "If-Match": version},
        )

    assert (await post(first_call, "addressed")).status_code == 200
    # Same key on another call: rejected, and the other call is untouched (not silently skipped).
    other = await post(second_call, "addressed")
    assert other.status_code == 422
    # Same key, same call, different body: also a client bug, not a retry.
    assert (await post(first_call, "following_up")).status_code == 422
    with db_engine.connect() as conn:
        status = conn.execute(
            text("SELECT workflow_status FROM calls WHERE id = :id"), {"id": second_call}
        ).scalar()
    assert status != "addressed"


async def test_replay_returns_the_original_answer_after_later_changes(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    [call_id] = _new_calls(db_engine, seed.clinic_a, 1)
    url = f"/v1/clinics/{seed.clinic_a}/calls/{call_id}/workflow"
    first_key = "key-" + uuid.uuid4().hex
    headers = {**_auth(RECEPTIONIST_A), "Idempotency-Key": first_key, "If-Match": "1"}
    first = await client.post(url, json={"status": "following_up"}, headers=headers)
    later = await client.post(
        url,
        json={"status": "addressed"},
        headers={**headers, "Idempotency-Key": "key-" + uuid.uuid4().hex, "If-Match": "2"},
    )
    assert later.json()["version"] == 3
    retry = await client.post(url, json={"status": "following_up"}, headers=headers)
    assert (
        retry.json()
        == first.json()
        == {
            "call_id": str(call_id),
            "workflow_status": "following_up",
            "version": 2,
        }
    )


async def test_call_list_is_audited_with_the_calls_shown(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    response = await client.get(
        f"/v1/clinics/{seed.clinic_a}/calls", params={"limit": 3}, headers=_auth(RECEPTIONIST_A)
    )
    shown = [item["id"] for item in response.json()["items"]]
    with db_engine.connect() as conn:
        detail = conn.execute(
            text(
                "SELECT detail FROM audit_log WHERE action = 'call.list' AND clinic_id = :c "
                "ORDER BY chain_seq DESC LIMIT 1"
            ),
            {"c": seed.clinic_a},
        ).scalar()
    assert detail == {"call_ids": shown}


def _cursor(value: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64-%%%",
        "é",
        _cursor([None, 5]),
        _cursor(["2026-09-01T00:00:00+00:00"]),
        _cursor({"a": 1, "b": 2}),
        _cursor(["not a date", str(uuid.uuid4())]),
        _cursor(["2026-09-01T00:00:00", str(uuid.uuid4())]),  # no timezone
        _cursor([None, "not-a-uuid"]),
        _cursor(42),
    ],
)
async def test_hostile_cursors_are_400_not_500(
    client: httpx.AsyncClient, seed: Seed, cursor: str
) -> None:
    response = await client.get(
        f"/v1/clinics/{seed.clinic_a}/calls", params={"cursor": cursor}, headers=_auth(ADMIN_AB)
    )
    assert response.status_code == 400


async def test_call_detail_exposes_only_the_documented_fields(
    client: httpx.AsyncClient, seed: Seed
) -> None:
    body = (
        await client.get(
            f"/v1/clinics/{seed.clinic_a}/calls/{seed.call_a}", headers=_auth(RECEPTIONIST_A)
        )
    ).json()
    assert set(body["call"]) == set(CallRecord.model_fields)
    assert "clinic_id" not in body["call"] and "local_dow" not in body["call"]


def test_openapi_describes_every_response() -> None:
    settings = CoreApiSettings(environment=Environment.TEST, auth_mode="test")
    schema = build_app(settings, engine=None).openapi()
    for path, method, model in (
        ("/v1/me", "get", "Me"),
        ("/v1/clinics/{clinic_id}/calls", "get", "CallPage"),
        ("/v1/clinics/{clinic_id}/calls/{call_id}", "get", "CallDetail"),
        ("/v1/clinics/{clinic_id}/calls/{call_id}/workflow", "post", "WorkflowResult"),
        ("/v1/clinics/{clinic_id}/analytics/summary", "get", "AnalyticsSummary"),
        ("/v1/clinics/{clinic_id}/usage", "get", "UsageReport"),
    ):
        ref = schema["paths"][path][method]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert ref["$ref"].endswith(f"/{model}"), (path, ref)


async def test_open_only_lists_just_the_calls_still_to_do(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    ids = _new_calls(db_engine, seed.clinic_b, 4)
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text("UPDATE calls SET workflow_status = 'addressed' WHERE id = ANY(:ids)"),
            {"ids": ids[:2]},
        )
        conn.execute(
            text("UPDATE calls SET workflow_status = 'following_up' WHERE id = :id"), {"id": ids[2]}
        )
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 1, "open_only": "true"}
        if cursor:
            params["cursor"] = cursor
        page = (
            await client.get(
                f"/v1/clinics/{seed.clinic_b}/calls", params=params, headers=_auth(ADMIN_AB)
            )
        ).json()
        assert all(i["workflow_status"] in ("pending", "following_up") for i in page["items"])
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert {str(ids[2]), str(ids[3])} <= set(seen)  # paged through, one at a time
    assert not {str(ids[0]), str(ids[1])} & set(seen)


async def test_urgent_messages_are_flagged_in_the_list_and_the_detail(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    urgent_call, routine_call = _new_calls(db_engine, seed.clinic_b, 2)
    with db_engine.connect() as conn, conn.begin():
        for call, category, urgent in (
            (urgent_call, "post_op", True),
            (urgent_call, "general", False),
            (routine_call, "urgent", False),  # the word alone doesn't make it urgent
        ):
            conn.execute(
                text(
                    "INSERT INTO messages "
                    "(clinic_id, provider_call_id, category, detail, urgent, dedupe_key) "
                    "VALUES (:c, :p, :cat, 'Synthetic message.', :u, :k)"
                ),
                {
                    "c": seed.clinic_b,
                    "p": f"call_{call.hex}",  # as _new_calls names them
                    "cat": category,
                    "u": urgent,
                    "k": uuid.uuid4().hex,
                },
            )
    items = (
        await client.get(
            f"/v1/clinics/{seed.clinic_b}/calls", params={"limit": 100}, headers=_auth(ADMIN_AB)
        )
    ).json()["items"]
    flags = {i["id"]: i["has_urgent_message"] for i in items}
    assert flags[str(urgent_call)] is True
    assert flags[str(routine_call)] is False

    detail = (
        await client.get(
            f"/v1/clinics/{seed.clinic_b}/calls/{urgent_call}", headers=_auth(ADMIN_AB)
        )
    ).json()
    assert detail["call"]["has_urgent_message"] is True
    assert sorted(m["urgent"] for m in detail["messages"]) == [False, True]
