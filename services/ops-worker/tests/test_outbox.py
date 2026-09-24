"""Outbox consumer against the migrated database, with the worker connected as app_ops."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from ops_worker.outbox import MAX_ATTEMPTS, process_batch
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import make_engine

from tests.support.database import Seed, as_role

pytestmark = pytest.mark.db


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str, str]] = []

    async def send(self, to: list[str], subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


class FailingSender:
    async def send(self, to: list[str], subject: str, body: str) -> None:
        raise RuntimeError("email_provider_down")


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


@pytest.fixture(scope="module", autouse=True)
def alert_contacts(db_engine: Engine, seed: Seed) -> None:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "wassup_owner", [seed.clinic_a])
        conn.execute(
            text("UPDATE clinics SET alert_contacts = CAST(:c AS jsonb) WHERE id = :id"),
            {
                "c": json.dumps(
                    ["alerts@example.test", {"email": "manager@example.test", "urgent": True}]
                ),
                "id": seed.clinic_a,
            },
        )


def _urgent_event(db_engine: Engine, clinic: uuid.UUID, detail: str, **event_overrides: Any) -> int:
    """An urgent message plus its outbox event, as voice-gateway would write them."""
    key = f"tool:{uuid.uuid4().hex}"
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "wassup_owner", [clinic])
        conn.execute(
            text(
                "INSERT INTO messages (clinic_id, provider_call_id, category, detail, callback_number, dedupe_key) "
                "VALUES (:c, 'call_test', 'urgent', :d, '+61400000555', :k)"
            ),
            {"c": clinic, "d": detail, "k": key},
        )
        columns = {"status": "pending", "attempts": 0, **event_overrides}
        return int(
            conn.execute(
                text(
                    "INSERT INTO outbox_events (clinic_id, event_type, dedupe_key, payload, status, attempts, available_at) "
                    "VALUES (:c, 'message.urgent', :k, '{}'::jsonb, :status, :attempts, "
                    "now() - interval '1 second') RETURNING id"
                ),
                {
                    "c": clinic,
                    "k": f"message.urgent:{key}",
                    "status": columns["status"],
                    "attempts": columns["attempts"],
                },
            ).scalar_one()
        )


def _event(db_engine: Engine, event_id: int) -> dict[str, Any]:
    with db_engine.connect() as conn:
        return dict(
            conn.execute(text("SELECT * FROM outbox_events WHERE id = :id"), {"id": event_id})
            .mappings()
            .one()
        )


def _mine(sender: FakeSender, marker: str) -> list[tuple[list[str], str, str]]:
    return [s for s in sender.sent if marker in s[2]]


async def test_urgent_message_is_emailed_once(
    ops_engine: AsyncEngine, db_engine: Engine, seed: Seed
) -> None:
    marker = f"urgent-{uuid.uuid4().hex[:8]}"
    event_id = _urgent_event(db_engine, seed.clinic_a, f"Wound concern {marker}")
    sender = FakeSender()
    await process_batch(ops_engine, sender)
    [(to, subject, body)] = _mine(sender, marker)
    assert set(to) == {"alerts@example.test", "manager@example.test"}
    assert "test-clinic-a" in subject
    assert marker not in subject and "+61400000555" not in subject  # no personal data in subjects
    assert "+61400000555" in body
    assert _event(db_engine, event_id)["status"] == "done"
    await process_batch(ops_engine, sender)
    assert len(_mine(sender, marker)) == 1


async def test_failure_backs_off_then_dead_letters(
    ops_engine: AsyncEngine, db_engine: Engine, seed: Seed
) -> None:
    event_id = _urgent_event(db_engine, seed.clinic_a, "Provider down test")
    await process_batch(ops_engine, FailingSender())
    row = _event(db_engine, event_id)
    assert (row["status"], row["attempts"], row["last_error"]) == (
        "pending",
        1,
        "email_provider_down",
    )

    last_try = _urgent_event(
        db_engine, seed.clinic_a, "Last attempt test", attempts=MAX_ATTEMPTS - 1
    )
    await process_batch(ops_engine, FailingSender())
    assert _event(db_engine, last_try)["status"] == "dead"


async def test_expired_lease_is_reclaimed(
    ops_engine: AsyncEngine, db_engine: Engine, seed: Seed
) -> None:
    marker = f"lease-{uuid.uuid4().hex[:8]}"
    event_id = _urgent_event(
        db_engine, seed.clinic_a, f"Crashed worker {marker}", status="processing", attempts=1
    )
    sender = FakeSender()
    await process_batch(ops_engine, sender)
    assert len(_mine(sender, marker)) == 1
    assert _event(db_engine, event_id)["status"] == "done"


async def test_already_sent_alert_is_not_resent(
    ops_engine: AsyncEngine, db_engine: Engine, seed: Seed
) -> None:
    """Crash after the email went out but before the event was marked done."""
    marker = f"sent-{uuid.uuid4().hex[:8]}"
    event_id = _urgent_event(db_engine, seed.clinic_a, f"Already sent {marker}")
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO notifications_sent (event_id, channel, status) VALUES (:id, 'email', 'sent')"
            ),
            {"id": event_id},
        )
    sender = FakeSender()
    await process_batch(ops_engine, sender)
    assert _mine(sender, marker) == []
    assert _event(db_engine, event_id)["status"] == "done"


async def test_clinic_without_alert_contacts_fails_loudly(
    ops_engine: AsyncEngine, db_engine: Engine, seed: Seed
) -> None:
    event_id = _urgent_event(db_engine, seed.clinic_b, "No contacts configured")
    await process_batch(ops_engine, FakeSender())
    assert _event(db_engine, event_id)["last_error"] == "no_alert_contacts"
