"""Raw-payload retention: only finished rows past the retention period are deleted."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from ops_worker import retention
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import make_engine

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


def _seed(db_engine: Engine, tag: str) -> None:
    rows = {
        # (event suffix, age in days, processed, error)
        "old_done": (100, True, None),
        "old_quarantined": (100, False, "quarantined:unknown_agent_or_number"),
        "old_open": (100, False, "processing_failed:KeyError"),  # never resolved: kept
        "new_done": (10, True, None),
    }
    with db_engine.connect() as conn, conn.begin():
        for name, (age, processed, error) in rows.items():
            conn.execute(
                text(
                    "INSERT INTO retell_events_raw (event, provider_call_id, payload, received_at, processed_at, error) "
                    "VALUES (:e, :c, '{}'::jsonb, now() - make_interval(days => :age), "
                    "CASE WHEN :p THEN now() END, :err)"
                ),
                {"e": name, "c": f"call_{tag}", "age": age, "p": processed, "err": error},
            )
            conn.execute(
                text(
                    "INSERT INTO tool_requests_raw (dedupe_key, tool, clinic_slug, provider_call_id, payload, "
                    "received_at, completed_at) VALUES (:k, 'capture_message', 's', :c, '{}'::jsonb, "
                    "now() - make_interval(days => :age), CASE WHEN :p THEN now() END)"
                ),
                {"k": f"{tag}:{name}", "c": f"call_{tag}", "age": age, "p": processed},
            )
        conn.execute(
            text(
                "INSERT INTO quarantine_events (reason, payload, received_at) VALUES "
                "(:r, '{}'::jsonb, now() - interval '100 days'), (:r, '{}'::jsonb, now())"
            ),
            {"r": f"test:{tag}"},
        )


def _left(db_engine: Engine, tag: str) -> tuple[set[str], set[str], int]:
    with db_engine.connect() as conn:
        events = set(
            conn.execute(
                text("SELECT event FROM retell_events_raw WHERE provider_call_id = :c"),
                {"c": f"call_{tag}"},
            ).scalars()
        )
        requests = {
            key.split(":", 1)[1]
            for key in conn.execute(
                text("SELECT dedupe_key FROM tool_requests_raw WHERE provider_call_id = :c"),
                {"c": f"call_{tag}"},
            ).scalars()
        }
        quarantined = conn.execute(
            text("SELECT count(*) FROM quarantine_events WHERE reason = :r"), {"r": f"test:{tag}"}
        ).scalar_one()
    return events, requests, int(quarantined)


async def test_only_finished_rows_past_retention_are_deleted(
    ops_engine: AsyncEngine, db_engine: Engine
) -> None:
    tag = uuid.uuid4().hex[:8]
    _seed(db_engine, tag)
    deleted = await retention.run(ops_engine, timedelta(days=90))
    assert all(count >= 1 for count in deleted.values())
    events, requests, quarantined = _left(db_engine, tag)
    assert events == {"old_open", "new_done"}  # unresolved rows wait for an operator
    assert requests == {"old_quarantined", "old_open", "new_done"}  # not completed = kept
    assert quarantined == 1
    assert await retention.run(ops_engine, timedelta(days=90)) == dict.fromkeys(deleted, 0)


async def test_batches_cover_large_backlogs(
    ops_engine: AsyncEngine, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag = uuid.uuid4().hex[:8]
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO quarantine_events (reason, payload, received_at) "
                "SELECT :r, '{}'::jsonb, now() - interval '200 days' FROM generate_series(1, 25)"
            ),
            {"r": f"test:{tag}"},
        )
    monkeypatch.setattr(retention, "BATCH", 4)
    await retention.run(ops_engine, timedelta(days=90))
    assert _left(db_engine, tag)[2] == 0
