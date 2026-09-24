"""Live event streams: one clinic only, resumable, and never outliving access."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from core_api import events
from core_api.main import build_app
from core_api.settings import CoreApiSettings
from core_api.staff import Staff
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import make_engine
from wassup_core.settings import Environment

from tests.support.database import Seed, as_role

pytestmark = pytest.mark.db

VIEWER = "test:uid-events-viewer:viewer@events.example.test"


@pytest.fixture(scope="module")
def viewer(db_engine: Engine, seed: Seed) -> None:
    with db_engine.connect() as conn, conn.begin():
        staff = conn.execute(
            text(
                "INSERT INTO staff_users (firebase_uid, email) VALUES ('uid-events-viewer', 'viewer@events.example.test') "
                "ON CONFLICT (firebase_uid) DO UPDATE SET email = EXCLUDED.email RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO clinic_memberships (clinic_id, staff_user_id, role) VALUES (:c, :s, 'viewer') "
                "ON CONFLICT DO NOTHING"
            ),
            {"c": seed.clinic_a, "s": staff},
        )


@pytest.fixture
async def core_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url, pool_size=3)

    @event.listens_for(engine.sync_engine, "connect")
    def _as_app_core(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("SET ROLE app_core")
        cursor.close()

    yield engine
    await engine.dispose()


def _app(engine: AsyncEngine, max_seconds: float = 0.6) -> httpx.AsyncClient:
    settings = CoreApiSettings(
        environment=Environment.TEST,
        auth_mode="test",
        events_poll_interval_s=0.05,
        events_max_seconds=max_seconds,
    )
    transport = httpx.ASGITransport(app=build_app(settings, engine=engine))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _emit(db_engine: Engine, clinic: uuid.UUID, event_type: str, payload: str = "{}") -> int:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "wassup_owner", [clinic])
        return int(
            conn.execute(
                text(
                    "INSERT INTO outbox_events (clinic_id, event_type, dedupe_key, payload, status) "
                    "VALUES (:c, :t, :k, CAST(:p AS jsonb), 'done') RETURNING id"
                ),
                {"c": clinic, "t": event_type, "k": f"test:{uuid.uuid4().hex}", "p": payload},
            ).scalar_one()
        )


def _parse(body: str) -> list[dict[str, str]]:
    frames = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if ": " in line and not line.startswith(":")
        )
        if "event" in fields:
            frames.append(fields)
    return frames


async def _read(
    client: httpx.AsyncClient, clinic: uuid.UUID, **headers: str
) -> list[dict[str, str]]:
    response = await client.get(
        f"/v1/clinics/{clinic}/events", headers={"Authorization": f"Bearer {VIEWER}", **headers}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return _parse(response.text)


async def test_resume_delivers_only_this_clinics_events_in_order(
    core_engine: AsyncEngine, db_engine: Engine, seed: Seed, viewer: None
) -> None:
    before = _emit(db_engine, seed.clinic_a, "call.analyzed", '{"call_id": "a0"}')
    first = _emit(db_engine, seed.clinic_a, "message.urgent", '{"call": "call_x"}')
    _emit(db_engine, seed.clinic_b, "message.urgent", '{"call": "call_other_clinic"}')
    second = _emit(db_engine, seed.clinic_a, "call.workflow", '{"call_id": "a1", "version": 2}')
    async with _app(core_engine) as client:
        frames = await _read(client, seed.clinic_a, **{"Last-Event-ID": str(before)})
    delivered = [(f["id"], f["event"]) for f in frames if "id" in f and f["event"] != "reauth"]
    assert delivered == [(str(first), "message.urgent"), (str(second), "call.workflow")]
    assert "call_other_clinic" not in str(frames)
    assert frames[-1]["event"] == "reauth"  # the stream ended at its time limit, on purpose


async def test_a_fresh_stream_starts_at_now(
    core_engine: AsyncEngine, db_engine: Engine, seed: Seed, viewer: None
) -> None:
    _emit(db_engine, seed.clinic_a, "call.analyzed")
    async with _app(core_engine) as client:
        frames = await _read(client, seed.clinic_a)
    assert [f["event"] for f in frames] == ["reauth"]  # history is fetched over REST, not replayed


async def test_far_behind_gets_a_reset_not_a_flood(
    core_engine: AsyncEngine,
    db_engine: Engine,
    seed: Seed,
    viewer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events, "MAX_BACKLOG", 2)
    start = _emit(db_engine, seed.clinic_a, "call.analyzed")
    for _ in range(3):
        _emit(db_engine, seed.clinic_a, "call.analyzed")
    async with _app(core_engine) as client:
        frames = await _read(client, seed.clinic_a, **{"Last-Event-ID": str(start)})
    assert [f["event"] for f in frames] == ["reset", "reauth"]


async def test_other_clinic_is_not_found(
    core_engine: AsyncEngine, seed: Seed, viewer: None
) -> None:
    async with _app(core_engine) as client:
        response = await client.get(
            f"/v1/clinics/{seed.clinic_b}/events", headers={"Authorization": f"Bearer {VIEWER}"}
        )
    assert response.status_code == 404


class _FakeRequest:
    def __init__(self, settings: CoreApiSettings) -> None:
        self.app = type("App", (), {"state": type("State", (), {"settings": settings})()})()

    async def is_disconnected(self) -> bool:
        return False


async def _collect(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


async def test_revoked_membership_ends_the_stream(
    core_engine: AsyncEngine, seed: Seed, viewer: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def not_a_member(*_args: Any) -> bool:
        return False

    monkeypatch.setattr(events, "_still_member", not_a_member)
    monkeypatch.setattr(events, "MEMBERSHIP_RECHECK_S", 0.0)
    settings = CoreApiSettings(environment=Environment.TEST, events_poll_interval_s=0.01)
    staff = Staff("uid-events-viewer", "v@example.test", uuid.uuid4(), {seed.clinic_a: "viewer"})
    chunks = await _collect(
        events._stream(_FakeRequest(settings), core_engine, staff, seed.clinic_a, None)  # type: ignore[arg-type]
    )
    assert "event: revoked" in chunks[-1]


async def test_stream_never_outlives_the_sign_in(core_engine: AsyncEngine, seed: Seed) -> None:
    settings = CoreApiSettings(environment=Environment.TEST, events_max_seconds=3600)
    staff = Staff(
        "uid-events-viewer",
        "v@example.test",
        uuid.uuid4(),
        {seed.clinic_a: "viewer"},
        expires_at=time.time() - 1,  # token already expired
    )
    started = time.monotonic()
    chunks = await _collect(
        events._stream(_FakeRequest(settings), core_engine, staff, seed.clinic_a, None)  # type: ignore[arg-type]
    )
    assert "event: reauth" in chunks[-1] and time.monotonic() - started < 1


@pytest.mark.parametrize("value", ["abc", "-5", "١٢", "9" * 40])
def test_resume_point_rejects_junk(value: str) -> None:
    assert events._resume_point(value) in (None, int("9" * 18))
