"""0009 backfills ``messages.urgent`` for every clinic and leaves row-level security forced."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from tests.support.database import migrate, migrated_database

pytestmark = pytest.mark.db


def test_backfill_marks_urgent_messages_in_every_clinic_and_restores_forced_rls() -> None:
    with migrated_database("0008") as engine:
        clinics = [uuid.uuid4(), uuid.uuid4()]
        with engine.begin() as conn:  # superuser: writes across clinics for the fixture
            org = conn.execute(
                text("INSERT INTO organizations (name) VALUES ('Test Org') RETURNING id")
            ).scalar_one()
            for i, clinic in enumerate(clinics):
                conn.execute(
                    text(
                        "INSERT INTO clinics (id, organization_id, slug, name, state) "
                        "VALUES (:id, :org, :slug, :slug, 'NSW')"
                    ),
                    {"id": clinic, "org": org, "slug": f"clinic-{i}"},
                )
                rows = [
                    (f"flagged-{i}", "post_op", True),  # urgent via its outbox event
                    (f"category-{i}", "Urgent", False),  # urgent via its category
                    (f"plain-{i}", "general", False),
                    (f"other-clinic-event-{i}", "general", False),
                ]
                for key, category, event in rows:
                    conn.execute(
                        text(
                            "INSERT INTO messages "
                            "(clinic_id, provider_call_id, category, detail, dedupe_key) "
                            "VALUES (:c, 'call_x', :cat, 'synthetic', :k)"
                        ),
                        {"c": clinic, "cat": category, "k": key},
                    )
                    if event:
                        conn.execute(
                            text(
                                "INSERT INTO outbox_events (clinic_id, event_type, dedupe_key) "
                                "VALUES (:c, 'message.urgent', :k)"
                            ),
                            {"c": clinic, "k": f"message.urgent:{key}"},
                        )
            # An urgent event recorded against a different clinic must not mark this message.
            conn.execute(
                text(
                    "INSERT INTO outbox_events (clinic_id, event_type, dedupe_key) "
                    "VALUES (:c, 'message.urgent', 'message.urgent:other-clinic-event-0')"
                ),
                {"c": clinics[1]},
            )

        migrate(engine, "0009")

        with engine.connect() as conn:
            urgent = dict(
                conn.execute(text("SELECT dedupe_key, urgent FROM messages")).tuples().all()
            )
            forced = dict(
                conn.execute(
                    text(
                        "SELECT relname, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('messages', 'outbox_events')"
                    )
                )
                .tuples()
                .all()
            )
        assert urgent == {
            "flagged-0": True,
            "category-0": True,
            "plain-0": False,
            "other-clinic-event-0": False,
            "flagged-1": True,
            "category-1": True,
            "plain-1": False,
            "other-clinic-event-1": False,
        }
        assert forced == {"messages": True, "outbox_events": True}
