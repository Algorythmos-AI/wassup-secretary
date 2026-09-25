"""wassup_core.db against the migrated test database, acting as the real app roles."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import async_url, clinic_array_literal, clinic_scope, make_engine, unscoped

from tests.support.database import Seed


def test_async_url_normalises_platform_urls() -> None:
    assert async_url("postgres://u:p@h:5432/d") == "postgresql+asyncpg://u:p@h:5432/d"
    assert async_url("postgresql://u@h/d") == "postgresql+asyncpg://u@h/d"


def test_clinic_literal_rejects_non_uuid_input() -> None:
    good = uuid.uuid4()
    assert clinic_array_literal([good]) == "{" + str(good) + "}"
    with pytest.raises(ValueError):
        clinic_array_literal(["1'); DROP TABLE calls; --"])


def _engine_as(db_url: str, role: str) -> AsyncEngine:
    engine = make_engine(db_url, pool_size=1)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_role(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute(f"SET ROLE {role}")
        cursor.close()

    return engine


@pytest.mark.db
async def test_clinic_scope_limits_rows_to_the_declared_clinic(db_url: str, seed: Seed) -> None:
    engine = _engine_as(db_url, "app_core")
    try:
        async with clinic_scope(engine, [seed.clinic_a]) as conn:
            ids = {r[0] for r in await conn.execute(text("SELECT id FROM calls"))}
        assert ids == {seed.call_a}
        async with unscoped(engine) as conn:
            count = (await conn.execute(text("SELECT count(*) FROM calls"))).scalar()
        assert count == 0
    finally:
        await engine.dispose()


@pytest.mark.db
async def test_scope_does_not_leak_to_the_next_transaction(db_url: str, seed: Seed) -> None:
    """set_config(..., true) is transaction-local: a pooled connection comes back clean."""
    engine = _engine_as(db_url, "app_core")  # pool of one: same physical connection reused
    try:
        async with clinic_scope(engine, [seed.clinic_a, seed.clinic_b]):
            pass
        async with unscoped(engine) as conn:
            count = (await conn.execute(text("SELECT count(*) FROM calls"))).scalar()
        assert count == 0
    finally:
        await engine.dispose()


@pytest.mark.db
async def test_statement_timeout_is_applied(db_url: str) -> None:
    engine = make_engine(db_url, pool_size=1, statement_timeout_ms=100)
    try:
        async with unscoped(engine) as conn:
            value = (await conn.execute(text("SHOW statement_timeout"))).scalar()
        assert value == "100ms"
    finally:
        await engine.dispose()


def test_engine_never_puts_query_parameters_in_errors() -> None:
    engine = make_engine("postgresql://u@localhost:1/db")
    assert engine.sync_engine.hide_parameters is True
