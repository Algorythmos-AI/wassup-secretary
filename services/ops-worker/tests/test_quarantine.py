"""Quarantine monitor: unresolved quarantined calls page ops and keep the health check red."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from ops_worker import quarantine
from ops_worker.main import build_app
from ops_worker.settings import OpsWorkerSettings
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import make_engine
from wassup_core.settings import Environment

pytestmark = pytest.mark.db


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str, str]] = []

    async def send(self, to: list[str], subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


@pytest.fixture
async def ops_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url, pool_size=2)

    @event.listens_for(engine.sync_engine, "connect")
    def _as_app_ops(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("SET ROLE app_ops")
        cursor.close()

    yield engine
    await engine.dispose()


def _quarantine(db_engine: Engine, reason: str, count: int, tag: str) -> None:
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO quarantine_events (reason, agent_id, payload) "
                'SELECT :r, :tag, \'{"call": {"transcript": "Test Patient words"}}\'::jsonb '
                "FROM generate_series(1, :n)"
            ),
            {"r": reason, "tag": tag, "n": count},
        )


def _resolve_all(db_engine: Engine) -> None:
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text(
                "UPDATE quarantine_events SET resolved_at = now(), resolution = 'test' WHERE resolved_at IS NULL"
            )
        )


async def test_new_quarantine_alerts_and_stays_red_until_resolved(
    ops_engine: AsyncEngine, db_engine: Engine
) -> None:
    _resolve_all(db_engine)
    tag = f"agent_{uuid.uuid4().hex[:8]}"
    monitor, sender = quarantine.QuarantineMonitor(["ops@example.test"]), FakeSender()
    assert await quarantine.tick(monitor, ops_engine, sender) == "ok"

    _quarantine(db_engine, "unknown_agent_or_number", 2, tag)
    assert await quarantine.tick(monitor, ops_engine, sender) == "failing"
    [(_to, _subject, body)] = sender.sent
    assert "unknown_agent_or_number: 2" in body and "Test Patient" not in body
    await quarantine.tick(monitor, ops_engine, sender)
    assert len(sender.sent) == 1  # nothing new: no repeat email

    _quarantine(db_engine, "tool_clinic_slug_mismatch:capture_message", 1, tag)
    await quarantine.tick(monitor, ops_engine, sender)
    assert len(sender.sent) == 2  # a new item: tell ops again
    assert monitor.report()["by_reason"] == {
        "tool_clinic_slug_mismatch": 1,
        "unknown_agent_or_number": 2,
    }

    settings = OpsWorkerSettings(environment=Environment.TEST, scheduler_enabled=False)
    app = build_app(settings, engine=ops_engine, retell=None)
    app.state.quarantine = monitor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://o") as c:
        assert (await c.get("/health/quarantine")).status_code == 503
        _resolve_all(db_engine)
        await quarantine.tick(monitor, ops_engine, sender)
        assert (await c.get("/health/quarantine")).status_code == 200
