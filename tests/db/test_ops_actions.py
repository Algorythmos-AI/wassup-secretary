"""db/ops_actions.py: the runbooks' operator decisions, made without a SQL session."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from types import ModuleType

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.support.database import ROOT, Seed, as_role

pytestmark = pytest.mark.db


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ops_actions", ROOT / "db" / "ops_actions.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ops = _load()


def _url(db_engine: Engine) -> str:
    return db_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


def _dead_event(db_engine: Engine, clinic: uuid.UUID) -> int:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "wassup_owner", [clinic])
        return int(
            conn.execute(
                text(
                    "INSERT INTO outbox_events (clinic_id, event_type, dedupe_key, status, attempts, "
                    "last_error) VALUES (:c, 'message.urgent', :k, 'dead', 8, 'ConnectError') "
                    "RETURNING id"
                ),
                {"c": clinic, "k": f"ops-test:{uuid.uuid4()}"},
            ).scalar_one()
        )


def _quarantine(db_engine: Engine, agent: str) -> int:
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO retell_events_raw (event, provider_call_id, agent_id, payload, error) "
                'VALUES (\'call_analyzed\', :p, :a, \'{"call": {"transcript": "Jane Citizen"}}\', '
                "'quarantined:unknown_agent_or_number')"
            ),
            {"p": f"call_q_{uuid.uuid4().hex}", "a": agent},
        )
        return int(
            conn.execute(
                text(
                    "INSERT INTO quarantine_events (reason, agent_id, payload) VALUES "
                    "('unknown_agent_or_number', :a, '{\"transcript\": \"Jane Citizen\"}') RETURNING id"
                ),
                {"a": agent},
            ).scalar_one()
        )


def _status(db_engine: Engine, event_id: int) -> str:
    with db_engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM outbox_events WHERE id = :i"), {"i": event_id}
            ).scalar_one()
        )


def test_list_shows_ids_and_codes_never_payloads(
    db_engine: Engine,
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event_id = _dead_event(db_engine, seed.clinic_a)
    q_id = _quarantine(db_engine, "agent_ops_list")
    monkeypatch.setenv("WASSUP_ADMIN_DATABASE_URL", _url(db_engine))
    monkeypatch.setenv("WASSUP_OPS_ACTION", "list")
    assert ops.main() == 0
    out = capsys.readouterr().out
    assert f"id={event_id}" in out and "last_error=ConnectError" in out and f"id={q_id}" in out
    assert "Jane" not in out and "payload" not in out


def test_requeue_is_a_dry_run_until_applied_and_audited(db_engine: Engine, seed: Seed) -> None:
    event_id = _dead_event(db_engine, seed.clinic_a)
    env = {"WASSUP_OPS_ACTION": "requeue-outbox", "WASSUP_OPS_IDS": str(event_id)}
    assert ops.run(_url(db_engine), env, apply=False)["ids"] == [event_id]
    assert _status(db_engine, event_id) == "dead"
    ops.run(_url(db_engine), env, apply=True)
    assert _status(db_engine, event_id) == "pending"
    with db_engine.connect() as conn:
        audited = conn.execute(
            text(
                "SELECT count(*) FROM audit_log WHERE action = 'outbox.requeued' AND target_id = :t"
            ),
            {"t": str(event_id)},
        ).scalar_one()
    assert audited == 1
    # It is no longer dead: a second requeue is refused and changes nothing.
    with pytest.raises(ops.OpsRefused, match="not dead-lettered"):
        ops.run(_url(db_engine), env, apply=True)


def test_one_wrong_id_refuses_the_whole_run(db_engine: Engine, seed: Seed) -> None:
    good = _dead_event(db_engine, seed.clinic_a)
    env = {"WASSUP_OPS_ACTION": "abandon-outbox", "WASSUP_OPS_IDS": f"{good},999999999"}
    with pytest.raises(ops.OpsRefused, match="999999999"):
        ops.run(_url(db_engine), env, apply=True)
    assert _status(db_engine, good) == "dead"
    ops.run(_url(db_engine), {**env, "WASSUP_OPS_IDS": str(good)}, apply=True)
    assert _status(db_engine, good) == "abandoned"
    with pytest.raises(ops.OpsRefused, match="numeric"):
        ops.run(_url(db_engine), {**env, "WASSUP_OPS_IDS": "1; DROP TABLE x"}, apply=True)


def test_quarantine_resolution_and_webhook_requeue(db_engine: Engine, seed: Seed) -> None:
    q_id = _quarantine(db_engine, "agent_ops_q")
    url = _url(db_engine)
    with pytest.raises(ops.OpsRefused, match="RESOLUTION"):
        ops.run(
            url,
            {"WASSUP_OPS_ACTION": "resolve-quarantine", "WASSUP_OPS_IDS": str(q_id)},
            apply=True,
        )
    requeued = ops.run(
        url,
        {"WASSUP_OPS_ACTION": "requeue-quarantined-webhooks", "WASSUP_OPS_AGENT": "agent_ops_q"},
        apply=True,
    )
    assert requeued["events"] == 1
    ops.run(
        url,
        {
            "WASSUP_OPS_ACTION": "resolve-quarantine",
            "WASSUP_OPS_IDS": str(q_id),
            "WASSUP_OPS_RESOLUTION": "replayed",
        },
        apply=True,
    )
    with db_engine.connect() as conn:
        row = conn.execute(
            text("SELECT resolution, resolved_at FROM quarantine_events WHERE id = :i"), {"i": q_id}
        ).one()
        raw_error = conn.execute(
            text("SELECT error FROM retell_events_raw WHERE agent_id = 'agent_ops_q'")
        ).scalar_one()
    assert row.resolution == "replayed" and row.resolved_at is not None
    assert raw_error == "processing_failed:requeued"


def test_production_apply_needs_the_action_as_acknowledgement(
    db_engine: Engine, seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = _dead_event(db_engine, seed.clinic_a)
    monkeypatch.setenv("WASSUP_ADMIN_DATABASE_URL", _url(db_engine))
    monkeypatch.setenv("WASSUP_OPS_ACTION", "abandon-outbox")
    monkeypatch.setenv("WASSUP_OPS_IDS", str(event_id))
    monkeypatch.setenv("WASSUP_OPS_APPLY", "true")
    monkeypatch.delenv("WASSUP_ENVIRONMENT", raising=False)
    assert ops.main() == 2 and _status(db_engine, event_id) == "dead"
    monkeypatch.setenv("WASSUP_PRODUCTION_ACK", "abandon-outbox")
    assert ops.main() == 0 and _status(db_engine, event_id) == "abandoned"
