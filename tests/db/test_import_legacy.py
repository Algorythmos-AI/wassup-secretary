"""db/import_legacy.py: a synthetic legacy database imported into a freshly migrated one."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.engine import Engine

from tests.support.database import ADMIN_URL, ROOT, migrated_database

pytestmark = pytest.mark.db

AGENT = "agent_legacy_ortho"
OTHER_AGENT = "agent_legacy_other"
PRACTICE = "Legacy Practice Name"

# Only the legacy columns the importer reads, with the legacy types.
LEGACY_SCHEMA = """
CREATE TABLE calls (
  call_id TEXT PRIMARY KEY, agent_id TEXT, practice TEXT, call_time TIMESTAMPTZ,
  caller_phone TEXT, patient_name TEXT, intent TEXT, call_summary TEXT, transcript TEXT,
  duration_seconds INTEGER, call_cost REAL, user_sentiment TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(), caller_name TEXT, caller_dob DATE, patient_id UUID,
  workflow_status TEXT DEFAULT 'pending'
);
CREATE TABLE w1_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), call_id TEXT, patient_id UUID,
  category TEXT NOT NULL, concern_type TEXT, detail TEXT NOT NULL, days_post_op INT,
  preferred_contact TEXT, callback_number TEXT, created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE w1_promises (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), call_id TEXT, patient_id UUID,
  promise_type TEXT NOT NULL, subject TEXT, due_at TIMESTAMPTZ NOT NULL, status TEXT DEFAULT 'open',
  created_at TIMESTAMPTZ DEFAULT NOW(), fulfilled_at TIMESTAMPTZ, fulfilled_by TEXT
);
CREATE TABLE call_interactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), call_id TEXT NOT NULL, action_type TEXT NOT NULL,
  status_from TEXT, status_to TEXT, note TEXT, actor_name TEXT, actor_role TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
"""
# 23:30 UTC on 21 Sep is 09:30 on Tuesday 22 Sep in Sydney: the clinic's date, not UTC's.
T0 = datetime(2026, 9, 21, 23, 30, tzinfo=UTC)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("import_legacy", ROOT / "db" / "import_legacy.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their annotations through it
    spec.loader.exec_module(module)
    return module


importer = _load()


@pytest.fixture(scope="module")
def target() -> Iterator[Engine]:
    with migrated_database() as engine:
        with engine.begin() as conn:
            org = conn.execute(
                text("INSERT INTO organizations (name) VALUES ('Test Org') RETURNING id")
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO clinics (organization_id, slug, name, state) "
                    "VALUES (:org, 'legacy-clinic', 'Legacy Clinic', 'NSW')"
                ),
                {"org": org},
            )
        yield engine


@pytest.fixture
def legacy() -> Iterator[Engine]:
    if not ADMIN_URL:
        pytest.skip("TEST_DATABASE_ADMIN_URL not set")
    admin_url = make_url(ADMIN_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    name = f"wassup_legacy_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(admin_url.set(database=name))
    with engine.begin() as conn:
        conn.connection.driver_connection.execute(LEGACY_SCHEMA)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _url(engine: Engine) -> str:
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


def _add_call(legacy: Engine, call_id: str, **values: Any) -> None:
    row = {
        "call_id": call_id,
        "agent_id": AGENT,
        "practice": None,
        "call_time": T0,
        "caller_phone": "+61400000555",
        "intent": "callback",
        "call_summary": "Synthetic caller asked for a callback.",
        "transcript": "Agent: Hello.\nUser: Synthetic.",
        "duration_seconds": 95,
        "call_cost": 0.1254,
        "user_sentiment": "Neutral",
        "workflow_status": "pending",
        "caller_name": "Synthetic Caller",
        **values,
    }
    _exec(
        legacy,
        "INSERT INTO calls (call_id, agent_id, practice, call_time, caller_phone, intent, "
        "call_summary, transcript, duration_seconds, call_cost, user_sentiment, workflow_status, "
        "caller_name) VALUES (:call_id, :agent_id, :practice, :call_time, :caller_phone, :intent, "
        ":call_summary, :transcript, :duration_seconds, :call_cost, :user_sentiment, "
        ":workflow_status, :caller_name)",
        **row,
    )


def _exec(engine: Engine, sql: str, **params: Any) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:  # superuser: reads across clinics for assertions
        return list(conn.execute(text(sql), params).mappings())


def _run(legacy: Engine, target: Engine, **kwargs: Any) -> Any:
    options = {"orphans": False, "apply": True, **kwargs}
    return importer.run(_url(legacy), _url(target), "legacy-clinic", [AGENT], [PRACTICE], **options)


def _clear(target: Engine) -> None:
    _exec(target, "DELETE FROM call_interactions")
    _exec(target, "DELETE FROM messages")
    _exec(target, "DELETE FROM promises")
    _exec(target, "DELETE FROM calls")


@pytest.fixture(autouse=True)
def _fresh_target(target: Engine) -> None:
    _clear(target)


def test_imports_one_clinic_faithfully_in_its_own_time_zone(legacy: Engine, target: Engine) -> None:
    _add_call(legacy, "call_a", workflow_status="addressed")
    _add_call(legacy, "call_old", agent_id=None, practice=PRACTICE)  # before agent ids existed
    _add_call(legacy, "call_other_clinic", agent_id=OTHER_AGENT)
    _exec(
        legacy,
        "INSERT INTO w1_messages (call_id, category, detail, concern_type, created_at) VALUES "
        "('call_a', 'urgent_doctor', 'Synthetic urgent detail.', NULL, :t), "
        "('call_a', 'post_op', 'Synthetic post-op detail.', 'swelling', :t), "
        "('call_other_clinic', 'general', 'Not this clinic.', NULL, :t)",
        t=T0,
    )
    _exec(
        legacy,
        "INSERT INTO w1_promises (call_id, promise_type, subject, due_at, status) "
        "VALUES ('call_a', 'callback', 'Synthetic subject', :t, 'done')",
        t=T0,
    )
    _exec(
        legacy,
        "INSERT INTO call_interactions (call_id, action_type, status_from, status_to, note, "
        "actor_name) VALUES ('call_a', 'status_change', 'pending', 'addressed', 'Rang back.', "
        "'Synthetic Staff')",
    )

    report = _run(legacy, target)

    calls = {
        r["provider_call_id"]: r
        for r in _rows(target, "SELECT * FROM calls WHERE source = 'import'")
    }
    assert set(calls) == {"call_a", "call_old"}
    a = calls["call_a"]
    assert a["cost_usd"] == Decimal("0.1254")  # legacy stored dollars already
    assert (str(a["local_date"]), a["local_hour"], a["local_dow"]) == ("2026-09-22", 9, 1)
    assert a["workflow_status"] == "addressed" and a["version"] == 1
    assert a["ended_at"] - a["started_at"] == timedelta(seconds=95)
    messages = {r["category"]: r for r in _rows(target, "SELECT * FROM messages ORDER BY category")}
    assert set(messages) == {"urgent_doctor", "post_op"}
    assert messages["urgent_doctor"]["urgent"] is True
    assert messages["post_op"]["urgent"] is False
    assert messages["post_op"]["metadata"] == {"concern_type": "swelling"}
    [promise] = _rows(target, "SELECT * FROM promises")
    assert promise["status"] == "fulfilled"
    [history] = _rows(target, "SELECT * FROM call_interactions")
    assert (history["status_to"], history["note"]) == ("addressed", "Rang back.")
    counts = report.counts
    assert counts["verified"] == 1 and counts["calls.inserted"] == 2
    assert counts["left_behind.calls.caller_name"] == 2
    assert counts["left_behind.promises.subject"] == 1
    assert counts["left_behind.interactions.actor_name"] == 1
    assert counts["messages.not_this_clinic_or_orphan_skipped"] == 1
    [audit] = _rows(target, "SELECT * FROM audit_log WHERE action = 'legacy.import'")
    assert audit["actor_service"] == "legacy-import"


def test_a_dry_run_verifies_then_leaves_nothing_behind(legacy: Engine, target: Engine) -> None:
    _add_call(legacy, "call_a")
    audits = "SELECT count(*) AS n FROM audit_log WHERE action = 'legacy.import'"
    before = _rows(target, audits)[0]["n"]
    report = _run(legacy, target, apply=False)
    assert report.counts["verified"] == 1
    assert _rows(target, "SELECT count(*) AS n FROM calls")[0]["n"] == 0
    assert _rows(target, audits)[0]["n"] == before


def test_running_again_adds_nothing_and_refreshes_only_untouched_imports(
    legacy: Engine, target: Engine
) -> None:
    _add_call(legacy, "call_untouched")
    _add_call(legacy, "call_changed_here")
    _add_call(legacy, "call_live")
    _exec(legacy, "INSERT INTO w1_messages (call_id, category, detail) VALUES "
          "('call_untouched', 'general', 'Synthetic.')")  # fmt: skip
    # Ingested live by the webhook before the import ran: this system's copy wins.
    clinic = _rows(target, "SELECT id FROM clinics WHERE slug = 'legacy-clinic'")[0]["id"]
    _exec(
        target,
        "INSERT INTO calls (clinic_id, provider_call_id, direction, summary, source) "
        "VALUES (:c, 'call_live', 'inbound', 'Live summary.', 'webhook')",
        c=clinic,
    )
    _run(legacy, target)
    # Staff changed one call here; meanwhile legacy staff changed both.
    _exec(
        target,
        "UPDATE calls SET workflow_status = 'following_up', version = 2 "
        "WHERE provider_call_id = 'call_changed_here'",
    )
    _exec(legacy, "UPDATE calls SET workflow_status = 'no_action_needed'")

    report = _run(legacy, target)

    status = {
        r["provider_call_id"]: (r["workflow_status"], r["summary"], r["source"])
        for r in _rows(target, "SELECT * FROM calls")
    }
    assert status["call_untouched"][0] == "no_action_needed"  # refreshed from legacy
    assert status["call_changed_here"][0] == "following_up"  # this system's change kept
    assert status["call_live"] == ("pending", "Live summary.", "webhook")  # never overwritten
    assert report.counts.get("calls.inserted", 0) == 0
    assert report.counts["calls.kept_live_or_changed_here"] == 2
    assert report.counts["messages.already_present"] == 1
    assert _rows(target, "SELECT count(*) AS n FROM messages")[0]["n"] == 1


def test_orphaned_messages_get_a_placeholder_call_only_when_asked(
    legacy: Engine, target: Engine
) -> None:
    _exec(
        legacy,
        "INSERT INTO w1_messages (call_id, category, detail, created_at) VALUES "
        "('call_lost_webhook', 'urgent_doctor', 'Synthetic urgent.', :t)",
        t=T0,
    )
    _add_call(legacy, "call_a")
    report = _run(legacy, target)
    assert report.counts["messages.not_this_clinic_or_orphan_skipped"] == 1
    assert _rows(target, "SELECT count(*) AS n FROM messages")[0]["n"] == 0

    report = _run(legacy, target, orphans=True)
    [placeholder] = _rows(
        target, "SELECT * FROM calls WHERE provider_call_id = 'call_lost_webhook'"
    )
    assert placeholder["started_at"] == T0 and placeholder["workflow_status"] == "pending"
    assert placeholder["summary"] is None and placeholder["analyzed_at"] is None
    [message] = _rows(target, "SELECT * FROM messages")
    assert message["provider_call_id"] == "call_lost_webhook" and message["urgent"] is True
    assert report.counts["calls.placeholder_for_orphans"] == 1


def test_overlong_text_is_clipped_to_the_column_limits(legacy: Engine, target: Engine) -> None:
    _add_call(legacy, "call_a")
    _exec(
        legacy,
        "INSERT INTO w1_messages (call_id, category, detail) VALUES ('call_a', 'general', :d)",
        d="x" * 5000,
    )
    report = _run(legacy, target)
    [message] = _rows(target, "SELECT detail FROM messages")
    assert len(message["detail"]) == 4000 and message["detail"].endswith("…")
    assert report.counts["messages.detail_truncated"] == 1


def test_a_failed_verification_commits_nothing(
    legacy: Engine, target: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_call(legacy, "call_a")
    monkeypatch.setattr(importer, "verify", lambda *_: ["calls: summary differs"])
    with pytest.raises(importer.ImportRefused) as refused:
        _run(legacy, target)
    assert refused.value.report.counts["verify failed, calls: summary differs"] == 1
    assert _rows(target, "SELECT count(*) AS n FROM calls")[0]["n"] == 0


def test_refuses_without_a_clinic_or_agents(legacy: Engine, target: Engine) -> None:
    with pytest.raises(importer.ImportRefused, match="agent ids"):
        importer.run(_url(legacy), _url(target), "legacy-clinic", [], [], orphans=False, apply=True)
    with pytest.raises(importer.ImportRefused, match="no clinic"):
        importer.run(_url(legacy), _url(target), "nope", [AGENT], [], orphans=False, apply=True)


def test_output_is_counts_only(
    legacy: Engine,
    target: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _add_call(legacy, "call_a")
    monkeypatch.setenv("WASSUP_LEGACY_DATABASE_URL", _url(legacy))
    monkeypatch.setenv("WASSUP_ADMIN_DATABASE_URL", _url(target))
    monkeypatch.setenv("WASSUP_IMPORT_CLINIC", "legacy-clinic")
    monkeypatch.setenv("WASSUP_IMPORT_AGENTS", AGENT)
    assert importer.main() == 0
    out = capsys.readouterr()
    assert "dry run" in out.out
    for private in ("+61400000555", "Synthetic Caller", "Synthetic caller asked", "Agent: Hello"):
        assert private not in out.out + out.err


def test_verification_catches_a_row_that_differs_from_the_source(
    legacy: Engine, target: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_call(legacy, "call_a")
    _exec(legacy, "INSERT INTO w1_messages (call_id, category, detail) VALUES "
          "('call_a', 'urgent_doctor', 'Synthetic.')")  # fmt: skip
    real_write = importer.write

    def corrupting_write(conn: Any, clinic_id: Any, plan: Any, report: Any) -> None:
        real_write(conn, clinic_id, plan, report)
        conn.execute("UPDATE calls SET intent = 'something else'")
        conn.execute("UPDATE messages SET urgent = false")

    monkeypatch.setattr(importer, "write", corrupting_write)
    with pytest.raises(importer.ImportRefused) as refused:
        _run(legacy, target)
    counts = refused.value.report.counts
    assert counts["verify failed, calls: intent differs"] == 1
    assert counts["verify failed, messages: urgent differs"] == 1
    assert _rows(target, "SELECT count(*) AS n FROM calls")[0]["n"] == 0


def test_production_apply_needs_a_per_clinic_acknowledgement(
    legacy: Engine,
    target: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _add_call(legacy, "call_a")
    monkeypatch.setenv("WASSUP_LEGACY_DATABASE_URL", _url(legacy))
    monkeypatch.setenv("WASSUP_ADMIN_DATABASE_URL", _url(target))
    monkeypatch.setenv("WASSUP_IMPORT_CLINIC", "legacy-clinic")
    monkeypatch.setenv("WASSUP_IMPORT_AGENTS", AGENT)
    monkeypatch.setenv("WASSUP_IMPORT_APPLY", "true")
    monkeypatch.delenv("WASSUP_ENVIRONMENT", raising=False)  # unset counts as production
    assert importer.main() == 2
    assert "WASSUP_PRODUCTION_ACK" in capsys.readouterr().err
    assert _rows(target, "SELECT count(*) AS n FROM calls")[0]["n"] == 0

    monkeypatch.setenv("WASSUP_PRODUCTION_ACK", "some-other-clinic")  # a stale flag is not consent
    assert importer.main() == 2
    monkeypatch.setenv(
        "WASSUP_ENVIRONMENT", "Production "
    )  # a typo is not a non-production environment
    assert importer.main() == 2
    assert _rows(target, "SELECT count(*) AS n FROM calls")[0]["n"] == 0

    monkeypatch.setenv("WASSUP_PRODUCTION_ACK", "legacy-clinic")
    assert importer.main() == 0
    assert _rows(target, "SELECT count(*) AS n FROM calls")[0]["n"] == 1
