"""Daily usage rollup (billing input)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from ops_worker import usage
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import make_engine

from tests.support.database import Seed, as_role

pytestmark = pytest.mark.db


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


def _call(
    db_engine: Engine, clinic: uuid.UUID, day: date, seconds: int | None, cost: str | None
) -> str:
    provider_id = f"call_usage_{uuid.uuid4().hex}"
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "wassup_owner", [clinic])
        conn.execute(
            text(
                "INSERT INTO calls (clinic_id, provider_call_id, direction, local_date, duration_seconds, cost_usd) "
                "VALUES (:c, :p, 'inbound', :d, :s, CAST(:cost AS numeric))"
            ),
            {"c": clinic, "p": provider_id, "d": day, "s": seconds, "cost": cost},
        )
    return provider_id


def _usage(db_engine: Engine, clinic: uuid.UUID, day: date) -> tuple[int, Decimal, Decimal] | None:
    with db_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT calls, minutes, provider_cost_usd FROM usage_daily WHERE clinic_id = :c AND day = :d"
            ),
            {"c": clinic, "d": day},
        ).first()
    return (row.calls, row.minutes, row.provider_cost_usd) if row else None


async def test_rollup_is_idempotent_and_absorbs_late_analysis(
    ops_engine: AsyncEngine, db_engine: Engine, seed: Seed
) -> None:
    today = datetime.now(UTC).date() - timedelta(days=1)
    with db_engine.connect() as conn, conn.begin():  # start from a clean slate for this day
        conn.execute(text("DELETE FROM usage_daily WHERE day = :d"), {"d": today})
        before = conn.execute(
            text(
                "SELECT count(*), coalesce(sum(duration_seconds), 0), coalesce(sum(cost_usd), 0) "
                "FROM calls WHERE clinic_id = :c AND local_date = :d"
            ),
            {"c": seed.clinic_b, "d": today},
        ).one()
    _call(db_engine, seed.clinic_b, today, 90, "0.1500")
    late = _call(db_engine, seed.clinic_b, today, None, None)  # analysis not in yet
    await usage.run(ops_engine)
    calls, minutes, cost = _usage(db_engine, seed.clinic_b, today) or (0, Decimal(0), Decimal(0))
    assert calls == before[0] + 2
    first = (calls, minutes, cost)
    await usage.run(ops_engine)
    assert _usage(db_engine, seed.clinic_b, today) == first  # idempotent

    with db_engine.connect() as conn, conn.begin():  # call_analyzed arrives later
        conn.execute(
            text(
                "UPDATE calls SET duration_seconds = 30, cost_usd = 0.05 WHERE provider_call_id = :p"
            ),
            {"p": late},
        )
    await usage.run(ops_engine)
    _, minutes_after, cost_after = _usage(db_engine, seed.clinic_b, today) or first
    assert minutes_after - minutes == Decimal("0.50")
    assert cost_after - cost == Decimal("0.0500")
