"""db/seed_synthetic.py and db/report.py against the migrated test database."""

from __future__ import annotations

import importlib.util
from types import ModuleType

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.support.database import ROOT

pytestmark = pytest.mark.db


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "db" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_synthetic = _load("seed_synthetic")
report = _load("report")


def test_seed_is_idempotent_and_adds_staff(db_engine: Engine, db_url: str) -> None:
    staff = seed_synthetic.parse_staff("uid-seed-1:seed@example.test:admin")
    seed_synthetic.seed(db_url, staff)
    seed_synthetic.seed(db_url, staff)
    with db_engine.connect() as conn:
        clinics = conn.execute(
            text("SELECT count(*) FROM clinics WHERE id = :id"), {"id": seed_synthetic.CLINIC_ID}
        ).scalar()
        role = conn.execute(
            text(
                "SELECT m.role FROM clinic_memberships m JOIN staff_users u ON u.id = m.staff_user_id "
                "WHERE u.firebase_uid = 'uid-seed-1'"
            )
        ).scalar()
        resolved = conn.execute(
            text("SELECT resolve_clinic_for_call(:a, :n)"),
            {"a": seed_synthetic.AGENT_ID, "n": seed_synthetic.NUMBER},
        ).scalar()
    assert (clinics, role, resolved) == (1, "admin", seed_synthetic.CLINIC_ID)


def test_seed_refuses_production_and_bad_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WASSUP_ENVIRONMENT", "production")
    assert seed_synthetic.main() == 2
    monkeypatch.delenv("WASSUP_ENVIRONMENT")
    assert seed_synthetic.main() == 2  # unset means production: fails closed
    with pytest.raises(SystemExit, match="unknown role"):
        seed_synthetic.parse_staff("uid:e@example.test:superuser")


def test_report_prints_counts_only(
    db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WASSUP_ADMIN_DATABASE_URL", db_url)
    assert report.main() == 0
    out = capsys.readouterr().out
    assert "schema_revision: 0008" in out and "calls:" in out
    assert "@" not in out  # no emails, names or other personal values
