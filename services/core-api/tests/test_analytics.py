"""Clinic analytics summary, against the migrated database as app_core."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable
from datetime import date

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

ANALYST = "test:uid-analyst:analyst@example.test"


@pytest.fixture(scope="module")
def clinic(db_engine: Engine, seed: Seed) -> uuid.UUID:
    """A clinic of its own (Brisbane: no daylight saving) with a known set of calls."""
    clinic_id = uuid.uuid4()
    with db_engine.connect() as conn, conn.begin():
        org = conn.execute(
            text("INSERT INTO organizations (name) VALUES ('Analytics Org') RETURNING id")
        ).scalar_one()
        as_role(conn, "wassup_owner", [clinic_id, seed.clinic_a])
        conn.execute(
            text(
                "INSERT INTO clinics (id, organization_id, slug, name, state, timezone) "
                "VALUES (:id, :o, :s, 'Analytics Clinic', 'QLD', 'Australia/Brisbane')"
            ),
            {"id": clinic_id, "o": org, "s": f"analytics-{clinic_id.hex[:8]}"},
        )
        rows = [
            # local_date, hour, dow, duration, cost, sentiment, intent, priority, workflow
            ("2026-09-01", 9, 1, 60, "0.1000", "Positive", "appointment", True, "pending"),
            ("2026-09-01", 9, 1, 120, "0.2000", "Negative", "appointment", False, "addressed"),
            ("2026-09-03", 14, 3, 30, "0.0525", None, "billing", False, "following_up"),
            ("2026-09-05", 23, 5, None, None, "neutral", None, False, "no_action_needed"),
            ("2026-08-31", 8, 0, 999, "9.0000", "Positive", "appointment", True, "pending"),  # out
        ]
        for i, (day, hour, dow, dur, cost, sent, intent, prio, wf) in enumerate(rows):
            conn.execute(
                text(
                    "INSERT INTO calls (clinic_id, provider_call_id, direction, local_date, local_hour, "
                    "local_dow, duration_seconds, cost_usd, sentiment, intent, is_priority, workflow_status) "
                    "VALUES (:c, :p, 'inbound', :d, :h, :w, :dur, CAST(:cost AS numeric), :s, :i, :prio, :wf)"
                ),
                {
                    "c": clinic_id,
                    "p": f"call_analytics_{clinic_id.hex[:6]}_{i}",
                    "d": date.fromisoformat(day),
                    "h": hour,
                    "w": dow,
                    "dur": dur,
                    "cost": cost,
                    "s": sent,
                    "i": intent,
                    "prio": prio,
                    "wf": wf,
                },
            )
        # A call in clinic A on an in-range day must never be counted here.
        conn.execute(
            text(
                "INSERT INTO calls (clinic_id, provider_call_id, direction, local_date) "
                "VALUES (:c, :p, 'inbound', '2026-09-02')"
            ),
            {"c": seed.clinic_a, "p": f"call_other_{uuid.uuid4().hex}"},
        )
        staff = conn.execute(
            text(
                "INSERT INTO staff_users (firebase_uid, email) VALUES ('uid-analyst', 'analyst@example.test') "
                "ON CONFLICT (firebase_uid) DO UPDATE SET email = EXCLUDED.email RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO clinic_memberships (clinic_id, staff_user_id, role) VALUES (:c, :s, 'viewer')"
            ),
            {"c": clinic_id, "s": staff},
        )
    return clinic_id


@pytest.fixture
async def client(db_url: str) -> AsyncIterator[httpx.AsyncClient]:
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


def _get(client: httpx.AsyncClient, clinic: uuid.UUID, **params: str) -> Awaitable[httpx.Response]:
    return client.get(
        f"/v1/clinics/{clinic}/analytics/summary",
        params=params,
        headers={"Authorization": f"Bearer {ANALYST}"},
    )


async def test_summary_totals_and_series(client: httpx.AsyncClient, clinic: uuid.UUID) -> None:
    response = await _get(client, clinic, **{"from": "2026-09-01", "to": "2026-09-07"})
    assert response.status_code == 200
    body = response.json()
    assert (body["from"], body["to"], body["timezone"]) == (
        "2026-09-01",
        "2026-09-07",
        "Australia/Brisbane",
    )
    assert body["totals"] == {
        "calls": 4,
        "avg_duration_seconds": 70,  # (60 + 120 + 30) / 3; a call without duration is not zero
        "total_duration_seconds": 210,
        "cost_usd": "0.35",
        "priority": 1,
        "reception_action": 0,
    }
    assert body["workflow"] == {
        "pending": 1,
        "following_up": 1,
        "addressed": 1,
        "no_action_needed": 1,
    }
    assert [d["calls"] for d in body["by_day"]] == [2, 0, 1, 0, 1, 0, 0]  # zero-filled, in order
    assert len(body["by_hour"]) == 24 and body["by_hour"][9] == {"hour": 9, "calls": 2}
    assert [d["calls"] for d in body["by_weekday"]] == [0, 2, 0, 1, 0, 1, 0]
    assert body["sentiment"] == {"positive": 1, "negative": 1, "neutral": 1, "unknown": 1}
    assert body["top_intents"] == [
        {"intent": "appointment", "calls": 2},
        {"intent": "billing", "calls": 1},
    ]


async def test_default_range_is_the_last_30_clinic_days(
    client: httpx.AsyncClient, clinic: uuid.UUID
) -> None:
    body = (await _get(client, clinic)).json()
    assert len(body["by_day"]) == 30
    assert date.fromisoformat(body["to"]) >= date(2026, 9, 1)


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-09-10", "to": "2026-09-01"},
        {"from": "2020-01-01", "to": "2026-09-01"},
        {"to": "0001-01-05"},  # the default 30-day window would start before year 1
    ],
)
async def test_invalid_ranges_are_400(
    client: httpx.AsyncClient, clinic: uuid.UUID, params: dict[str, str]
) -> None:
    assert (await _get(client, clinic, **params)).status_code == 400


async def test_other_clinics_are_not_found(
    client: httpx.AsyncClient, clinic: uuid.UUID, seed: Seed
) -> None:
    assert (await _get(client, seed.clinic_a)).status_code == 404
