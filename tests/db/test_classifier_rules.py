"""db/classifier_rules.py: load, activate (rollback) and reclassify, as db-admin does.

The database is shared by the whole session, so every expectation is relative to what earlier
tests left behind (rule versions, calls, audit rows) rather than to an empty table.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from types import ModuleType
from typing import Any

import pytest
from psycopg import Connection, connect
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.support.database import ROOT, Seed, as_role

pytestmark = pytest.mark.db


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "classifier_rules", ROOT / "db" / "classifier_rules.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load()
V1 = json.dumps({"tiers": [{"level": "priority_1", "reason": "Clinical", "any": ["swelling"]}]})
V2 = json.dumps({"tiers": [{"level": "priority_3", "reason": "Routine", "any": ["swelling"]}]})
MAX_VERSION = "SELECT coalesce(max(version), 0) FROM clinic_classifier_rules WHERE clinic_id = :c"
RULE_ROWS = "SELECT count(*) FROM clinic_classifier_rules WHERE clinic_id = :c"
ACTIVE_VERSION = "SELECT version FROM clinic_classifier_rules WHERE clinic_id = :c AND active"
AUDITS = "SELECT count(*) FROM audit_log WHERE action = 'calls.reclassify' AND clinic_id = :c"
DEACTIVATE = "UPDATE clinic_classifier_rules SET active = false WHERE clinic_id = %s"


def _conn(db_engine: Engine) -> Connection[Any]:
    url = db_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    return connect(url, row_factory=dict_row)


def _scalar(db_engine: Engine, sql: str, clinic: uuid.UUID) -> Any:
    with db_engine.connect() as conn:
        return conn.execute(text(sql), {"c": clinic}).scalar_one()


def _slug(db_engine: Engine, clinic: uuid.UUID) -> str:
    return str(_scalar(db_engine, "SELECT slug FROM clinics WHERE id = :c", clinic))


def _add_call(db_engine: Engine, clinic: uuid.UUID, summary: str) -> uuid.UUID:
    call_id = uuid.uuid4()
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "wassup_owner", [clinic])
        conn.execute(
            text(
                "INSERT INTO calls (id, clinic_id, provider_call_id, direction, summary, analyzed_at) "
                "VALUES (:id, :c, :p, 'inbound', :s, now())"
            ),
            {"id": call_id, "c": clinic, "p": f"call_{call_id.hex}", "s": summary},
        )
    return call_id


def _level(db_engine: Engine, call_id: uuid.UUID) -> tuple[str | None, bool, int | None]:
    with db_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT priority_level, is_priority, classifier_version FROM calls WHERE id = :id"
            ),
            {"id": call_id},
        ).one()
    return row.priority_level, row.is_priority, row.classifier_version


def test_load_activate_reclassify_and_roll_back(db_engine: Engine, seed: Seed) -> None:
    clinic, slug = seed.clinic_a, _slug(db_engine, seed.clinic_a)
    base = int(_scalar(db_engine, MAX_VERSION, clinic))
    audits_before = int(_scalar(db_engine, AUDITS, clinic))
    swollen = _add_call(db_engine, clinic, "swelling after surgery")
    other = _add_call(db_engine, seed.clinic_b, "swelling after surgery")  # untouched
    with _conn(db_engine) as conn:
        assert tool.load(conn, slug, V1, "first", activate=False) == base + 1
        assert _level(db_engine, swollen) == (None, False, None)  # inactive: nothing changes
        tool.activate(conn, slug, base + 1)
        totals = tool.reclassify(conn, slug)
    assert totals["changed"] >= 1 and totals["clinics"] == 1
    assert _level(db_engine, swollen) == ("priority_1", True, base + 1)
    assert _level(db_engine, other) == (None, False, None)

    with _conn(db_engine) as conn:
        assert tool.load(conn, slug, V2, None, activate=True) == base + 2
        tool.reclassify(conn, slug)
    assert _level(db_engine, swollen) == ("priority_3", False, base + 2)

    # Rollback: activate the earlier version again; reclassify is idempotent the second time.
    with _conn(db_engine) as conn:
        tool.activate(conn, slug, base + 1)
        first = tool.reclassify(conn, slug)
        second = tool.reclassify(conn, slug)
    assert _level(db_engine, swollen) == ("priority_1", True, base + 1)
    assert first["changed"] >= 1
    assert second["changed"] == 0 and second["unchanged"] == first["calls"]

    assert _scalar(db_engine, ACTIVE_VERSION, clinic) == base + 1
    assert int(_scalar(db_engine, AUDITS, clinic)) == audits_before + 4

    with _conn(db_engine) as conn:  # leave the clinic as found for later tests
        conn.execute(DEACTIVATE, (clinic,))
        conn.commit()


def test_invalid_rules_are_refused_before_any_write(db_engine: Engine, seed: Seed) -> None:
    clinic, slug = seed.clinic_b, _slug(db_engine, seed.clinic_b)
    before = int(_scalar(db_engine, RULE_ROWS, clinic))
    bad = json.dumps({"tiers": [{"level": "nope", "reason": "x", "any": ["a"]}]})
    with _conn(db_engine) as conn, pytest.raises(ValueError):
        tool.load(conn, slug, bad, None, True)
    assert int(_scalar(db_engine, RULE_ROWS, clinic)) == before


def test_activating_a_missing_version_changes_nothing(db_engine: Engine, seed: Seed) -> None:
    clinic, slug = seed.clinic_b, _slug(db_engine, seed.clinic_b)
    with _conn(db_engine) as conn:
        version = tool.load(conn, slug, V1, None, activate=True)
        with pytest.raises(SystemExit):
            tool.activate(conn, slug, 9999)
    assert _scalar(db_engine, ACTIVE_VERSION, clinic) == version
    with _conn(db_engine) as conn:
        conn.execute(DEACTIVATE, (clinic,))
        conn.commit()
