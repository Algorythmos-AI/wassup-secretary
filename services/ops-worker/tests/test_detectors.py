"""Outage detectors: the daily line check (canary) and the ingestion-gap reconciler."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from ops_worker import canary, reconcile
from ops_worker.main import build_app as build_ops_app
from ops_worker.settings import OpsWorkerSettings
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from voice_gateway.main import build_app as build_voice_app
from voice_gateway.settings import VoiceGatewaySettings
from voice_gateway.signature import sign
from wassup_core.db import make_engine
from wassup_core.settings import Environment

from tests.support.database import Seed

# 07:31 on Tue 22 Sep 2026 in Sydney = 21:31 UTC on the 21st (AEST, UTC+10).
MORNING = datetime(2026, 9, 21, 21, 31, tzinfo=UTC)


def _lines() -> list[str]:
    """Unique synthetic line numbers per test (canary_runs is shared across tests)."""
    base = random.randint(10_000, 99_999)  # noqa: S311 — test data, not security
    return [f"+614009{base}", f"+614008{base}"]


def _cfg(lines: list[str], **overrides: Any) -> canary.CanaryConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "agent_id": "agent_line_check",
        "agent_version": 0,
        "local_time": "07:30",
        "timezone": "Australia/Sydney",
        "lines": lines,
        "ops_emails": ["ops@example.test"],
    }
    values.update(overrides)
    return canary.CanaryConfig(**values)


class FakeRetell:
    def __init__(self, calls: list[dict[str, Any]] | None = None, fail_place: bool = False) -> None:
        self.placed: list[tuple[str, str]] = []
        self.calls = calls or []
        self.fail_place = fail_place

    async def create_phone_call(
        self, from_number: str, to_number: str, agent_id: str, agent_version: int | None
    ) -> str | None:
        if self.fail_place:
            raise RuntimeError("telephony_provider_permission_denied")
        self.placed.append((from_number, to_number))
        return f"call_canary_{len(self.placed)}"

    async def list_calls(self, agent_ids: list[str], limit: int) -> list[dict[str, Any]]:
        return self.calls


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str, str]] = []

    async def send(self, to: list[str], subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


# ---------- pure logic ----------


def test_each_line_is_called_from_another_line() -> None:
    assert canary.routes(["+61A", "+61B"]) == [("+61A", "+61B"), ("+61B", "+61A")]
    assert canary.routes(["+61A"]) == []


def test_due_follows_sydney_time_including_daylight_saving() -> None:
    cfg = _cfg(["+61A", "+61B"])
    assert canary.local_date_if_due(cfg, MORNING - timedelta(minutes=2)) is None  # 07:29 AEST
    assert canary.local_date_if_due(cfg, MORNING) == "2026-09-22"
    aedt_0730 = datetime(2026, 10, 5, 20, 30, tzinfo=UTC)  # 07:30 AEDT (UTC+11) on 6 Oct
    assert canary.local_date_if_due(cfg, aedt_0730) == "2026-10-06"
    assert canary.local_date_if_due(cfg, aedt_0730 - timedelta(minutes=1)) is None


def test_health_verdicts() -> None:
    cfg = _cfg(["+61A", "+61B"])
    now = MORNING + timedelta(hours=2)

    def run(line: str, status: str, placed_ago: timedelta) -> dict[str, Any]:
        return {
            "to_number": line,
            "run_date": "2026-09-22",
            "status": status,
            "placed_at": now - placed_ago,
        }

    ok = canary.assess(
        [run("+61A", "received", timedelta(hours=1)), run("+61B", "late", timedelta(hours=1))],
        cfg,
        now,
    )
    assert ok["status"] == "ok"
    pending = canary.assess(
        [run("+61A", "placed", timedelta(minutes=5)), run("+61B", "received", timedelta(hours=1))],
        cfg,
        now,
    )
    assert pending["status"] == "ok"
    assert (
        canary.assess(
            [
                run("+61A", "failed", timedelta(hours=1)),
                run("+61B", "received", timedelta(hours=1)),
            ],
            cfg,
            now,
        )["status"]
        == "failing"
    )
    assert (
        canary.assess([run("+61B", "received", timedelta(hours=1))], cfg, now)["status"]
        == "failing"
    )  # line A never ran
    stale = canary.assess(
        [
            run("+61A", "received", timedelta(hours=30)),
            run("+61B", "received", timedelta(hours=30)),
        ],
        cfg,
        now,
    )
    assert stale["status"] == "failing"
    assert canary.assess([], _cfg(["+61A"], enabled=False), now)["status"] == "disabled"


# ---------- against the database ----------


def _engine_as(db_url: str, role: str) -> AsyncEngine:
    engine = make_engine(db_url, pool_size=2)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_role(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute(f"SET ROLE {role}")
        cursor.close()

    return engine


@pytest.fixture
async def ops_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = _engine_as(db_url, "app_ops")
    yield engine
    await engine.dispose()


def _runs(
    db_engine: Engine, lines: list[str], run_date: str = "2026-09-22"
) -> dict[str, dict[str, Any]]:
    """The runs of one local day (MORNING's by default). Ticks at the real "now" legitimately
    start today's runs too once 07:30 Sydney has passed, so never mix days."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM canary_runs WHERE to_number = ANY(:l) AND run_date = :d"),
            {"l": lines, "d": date.fromisoformat(run_date)},
        ).mappings()
        return {r["to_number"]: dict(r) for r in rows}


@pytest.mark.db
async def test_tick_places_each_line_once_per_day(
    ops_engine: AsyncEngine, db_engine: Engine
) -> None:
    lines = _lines()
    retell = FakeRetell()
    await canary.tick(ops_engine, retell, FakeSender(), _cfg(lines), MORNING)
    await canary.tick(ops_engine, retell, FakeSender(), _cfg(lines), MORNING + timedelta(minutes=1))
    assert sorted(retell.placed) == sorted([(lines[1], lines[0]), (lines[0], lines[1])])
    assert {r["status"] for r in _runs(db_engine, lines).values()} == {"placed"}


@pytest.mark.db
async def test_receipt_via_voice_gateway_then_overdue_line_alerts_once(
    ops_engine: AsyncEngine, db_engine: Engine, db_url: str, seed: Seed
) -> None:
    lines = _lines()
    # Placed 20 minutes before "now" so the receipt window has passed for any line not received.
    placed_at = datetime.now(UTC) - timedelta(minutes=20)
    await canary.tick(ops_engine, FakeRetell(), FakeSender(), _cfg(lines), MORNING)
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text("UPDATE canary_runs SET placed_at = :p WHERE to_number = ANY(:l)"),
            {"p": placed_at, "l": lines},
        )

    # The synthetic call to line 0 arrives at voice-gateway (running as app_voice).
    voice_engine = _engine_as(db_url, "app_voice")
    settings = VoiceGatewaySettings(
        environment=Environment.TEST, retell_api_key="k", ai_line_numbers=",".join(lines)
    )
    body = json.dumps(
        {
            "event": "call_analyzed",
            "call": {
                "call_id": "call_line_check_in",
                "agent_id": "agent_test_a",
                "direction": "inbound",
                "from_number": lines[1],
                "to_number": lines[0],
            },
        }
    ).encode()
    transport = httpx.ASGITransport(app=build_voice_app(settings, engine=voice_engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/retell/webhook",
            content=body,
            headers={"x-retell-signature": sign(body, "k", int(time.time() * 1000))},
        )
    await voice_engine.dispose()
    assert response.status_code == 204

    sender = FakeSender()
    now = datetime.now(UTC)
    await canary.tick(ops_engine, FakeRetell(), sender, _cfg(lines), now)
    await canary.tick(ops_engine, FakeRetell(), sender, _cfg(lines), now + timedelta(minutes=1))
    runs = _runs(db_engine, lines)
    assert runs[lines[0]]["status"] == "received"
    assert (runs[lines[1]]["status"], runs[lines[1]]["error"]) == ("failed", "not_received")
    # The overdue sweep is global (other tests' runs may alert too); count only this test's lines.
    mine = [subject for _, subject, _ in sender.sent if lines[0] in subject or lines[1] in subject]
    assert mine == [f"WASSUP line check FAILED ({lines[1]})"]


@pytest.mark.db
async def test_place_failure_is_recorded_and_alerted(
    ops_engine: AsyncEngine, db_engine: Engine
) -> None:
    lines = _lines()
    sender = FakeSender()
    await canary.tick(ops_engine, FakeRetell(fail_place=True), sender, _cfg(lines), MORNING)
    runs = _runs(db_engine, lines)
    assert {r["status"] for r in runs.values()} == {"failed"}
    assert (
        sum(1 for _, subject, _ in sender.sent if lines[0] in subject or lines[1] in subject) == 2
    )


@pytest.mark.db
async def test_canary_health_endpoint(ops_engine: AsyncEngine) -> None:
    lines = _lines()
    settings = OpsWorkerSettings(
        environment=Environment.TEST,
        scheduler_enabled=False,
        canary_enabled=True,
        canary_agent_id="agent_line_check",
        ai_line_numbers=",".join(lines),
    )
    app = build_ops_app(settings, engine=ops_engine, retell=FakeRetell())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        never_ran = await client.get("/health/canary")
    assert never_ran.status_code == 503  # no run at all is itself a failure (dead scheduler)
    assert {line["state"] for line in never_ran.json()["lines"]} == {"stale"}


@pytest.mark.db
async def test_ingestion_gap_detects_missing_calls(ops_engine: AsyncEngine, seed: Seed) -> None:
    ended = (datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000
    recent = (datetime.now(UTC) - timedelta(minutes=2)).timestamp() * 1000
    calls = [
        {
            "call_id": "call_test-clinic-a",
            "call_status": "ended",
            "end_timestamp": ended,
            "direction": "inbound",
        },
        {
            "call_id": "call_never_ingested",
            "call_status": "ended",
            "end_timestamp": ended,
            "direction": "inbound",
        },
        {
            "call_id": "call_synthetic",
            "call_status": "ended",
            "end_timestamp": ended,
            "direction": "outbound",
        },
        {
            "call_id": "call_still_settling",
            "call_status": "ended",
            "end_timestamp": recent,
            "direction": "inbound",
        },
        {"call_id": "call_unanswered", "call_status": "not_connected", "end_timestamp": ended},
    ]
    report = await reconcile.ingestion_gap(ops_engine, FakeRetell(calls), set(), datetime.now(UTC))
    assert report == {"ingestion": "gap", "checked_calls": 2, "missing_calls": 1}
    healthy = await reconcile.ingestion_gap(
        ops_engine, FakeRetell(calls[:1]), set(), datetime.now(UTC)
    )
    assert healthy["ingestion"] == "ok"


@pytest.mark.db
async def test_freshness_probe_is_single_flight_and_cached(ops_engine: AsyncEngine) -> None:
    """The endpoint is unauthenticated: a burst of requests must reach the provider once."""

    class CountingRetell(FakeRetell):
        def __init__(self) -> None:
            super().__init__()
            self.list_calls_count = 0

        async def list_calls(self, agent_ids: list[str], limit: int) -> list[dict[str, Any]]:
            self.list_calls_count += 1
            await asyncio.sleep(0.05)  # a slow provider makes concurrent requests overlap
            return []

    retell = CountingRetell()
    settings = OpsWorkerSettings(environment=Environment.TEST, scheduler_enabled=False)
    app = build_ops_app(settings, engine=ops_engine, retell=retell)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(*(client.get("/health/freshness") for _ in range(20)))
        responses.append(await client.get("/health/freshness"))
    assert {r.status_code for r in responses} == {200}
    assert retell.list_calls_count == 1
