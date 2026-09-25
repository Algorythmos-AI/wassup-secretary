"""db/import_patients.py: a synthetic patient CSV into a clinic, as db-admin does."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import uuid
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.support.database import ROOT, Seed

pytestmark = pytest.mark.db


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "import_patients", ROOT / "db" / "import_patients.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load()

CSV = b"""\xef\xbb\xbfsource_pms_id,first_name,last_name,date_of_birth,phone,is_deceased,extra
P1,Test,Patient,1970-01-02,0412 345 678,no,ignored
P2,Second,Person,03/04/1985,+61 2 9876 5432,,x
P3,Late,Person,1950-12-31,,yes,
P4,,Nameless,1960-01-01,,no,
P5,Bad,Date,31/31/1999,,no,
P1,Dup,Licate,1970-01-02,,no,
"""


def _url(db_engine: Engine) -> str:
    return db_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


def _slug(db_engine: Engine, clinic: uuid.UUID) -> str:
    with db_engine.connect() as conn:
        return str(
            conn.execute(text("SELECT slug FROM clinics WHERE id = :c"), {"c": clinic}).scalar_one()
        )


def _patients(db_engine: Engine, clinic: uuid.UUID) -> dict[str, Any]:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM patients WHERE clinic_id = :c ORDER BY source_pms_id"),
            {"c": clinic},
        ).mappings()
        return {r["source_pms_id"]: dict(r) for r in rows}


def test_parses_normalises_and_refuses_unusable_rows() -> None:
    rows, refused = tool.parse_rows(CSV)
    assert [r["source_pms_id"] for r in rows] == ["P1", "P2", "P3"]
    assert refused == ["P4", "P5", "P1"]  # blank name, bad date, duplicate id
    by = {r["source_pms_id"]: r for r in rows}
    assert by["P1"]["phone"] == "+61412345678" and by["P1"]["date_of_birth"] == date(1970, 1, 2)
    assert by["P2"]["phone"] == "+61298765432" and by["P2"]["date_of_birth"] == date(1985, 4, 3)
    assert by["P3"]["is_deceased"] is True and by["P3"]["phone"] is None


def test_dry_run_verifies_and_writes_nothing(db_engine: Engine, seed: Seed) -> None:
    before = _patients(db_engine, seed.clinic_b)
    counts = tool.run(_url(db_engine), _slug(db_engine, seed.clinic_b), CSV, apply=False)
    assert counts["inserted"] == 3 and counts["refused"] == 3
    assert _patients(db_engine, seed.clinic_b) == before


def test_apply_then_refresh_is_idempotent_and_never_deletes(db_engine: Engine, seed: Seed) -> None:
    clinic, slug = seed.clinic_b, _slug(db_engine, seed.clinic_b)
    first = tool.run(_url(db_engine), slug, CSV, apply=True)
    assert (first["inserted"], first["refreshed"], first["unchanged"]) == (3, 0, 0)
    stored = _patients(db_engine, clinic)
    assert stored["P1"]["phone"] == "+61412345678" and stored["P3"]["is_deceased"] is True

    again = tool.run(_url(db_engine), slug, CSV, apply=True)
    assert (again["inserted"], again["refreshed"], again["unchanged"]) == (0, 0, 3)

    # A changed number is refreshed; a patient missing from the export is kept.
    changed = CSV.replace(b"0412 345 678", b"0498 765 432").replace(
        b"P3,Late,Person,1950-12-31,,yes,\n", b""
    )
    third = tool.run(_url(db_engine), slug, changed, apply=True)
    assert (third["inserted"], third["refreshed"], third["unchanged"]) == (0, 1, 1)
    stored = _patients(db_engine, clinic)
    assert stored["P1"]["phone"] == "+61498765432" and "P3" in stored
    assert stored["P1"]["updated_at"] > stored["P2"]["updated_at"]
    assert (
        _patients(db_engine, seed.clinic_a).keys().isdisjoint(stored.keys())
    )  # other clinic untouched
    with db_engine.connect() as conn:
        audits = conn.execute(
            text(
                "SELECT count(*) FROM audit_log WHERE action = 'patients.import' AND clinic_id = :c"
            ),
            {"c": clinic},
        ).scalar_one()
    assert audits >= 3


def test_refuses_bad_files_and_mismatched_checksums(tmp_path: Path) -> None:
    with pytest.raises(tool.ImportRefused, match="missing columns"):
        tool.parse_rows(b"source_pms_id,first_name\nP1,x\n")
    path = tmp_path / "p.csv"
    path.write_bytes(CSV)
    assert tool.read_file(str(path), None, hashlib.sha256(CSV).hexdigest()) == CSV
    with pytest.raises(tool.ImportRefused, match="SHA-256"):
        tool.read_file(str(path), None, "00" * 32)
    with pytest.raises(tool.ImportRefused, match="https"):
        tool.read_file(None, "http://example.test/p.csv", "00" * 32)
    with pytest.raises(tool.ImportRefused, match="SHA256"):
        tool.read_file(None, "https://example.test/p.csv", None)


def test_month_first_dates_refuse_the_whole_file() -> None:
    month_first = CSV.replace(b"1970-01-02", b"05/31/1970")
    with pytest.raises(tool.ImportRefused, match="month-first"):
        tool.parse_rows(month_first)
    # An ambiguous slash date alone is read day-first, as documented.
    rows, _ = tool.parse_rows(CSV)
    assert next(r for r in rows if r["source_pms_id"] == "P2")["date_of_birth"] == date(1985, 4, 3)


def test_url_fetch_refuses_redirects_and_oversized_files() -> None:
    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://example.test/p.csv"})

    with pytest.raises(tool.ImportRefused, match="redirects"):
        tool.fetch("https://example.test/p.csv", httpx.MockTransport(redirect))

    def huge(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (tool.MAX_FILE_BYTES + 1))

    with pytest.raises(tool.ImportRefused, match="larger"):
        tool.fetch("https://example.test/p.csv", httpx.MockTransport(huge))

    def ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=CSV)

    assert tool.fetch("https://example.test/p.csv", httpx.MockTransport(ok)) == CSV


def test_admin_url_requires_tls_off_the_private_network() -> None:
    assert "sslmode=require" in tool._url("postgresql://u:p@db.example.test:5432/x")
    assert "sslmode" not in tool._url("postgresql://u@localhost:5432/x")
    assert "sslmode" not in tool._url("postgresql://u@postgres.railway.internal:5432/x")


def test_production_apply_needs_the_clinic_acknowledgement(
    db_engine: Engine,
    seed: Seed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "p.csv"
    path.write_bytes(CSV)
    monkeypatch.setenv("WASSUP_ADMIN_DATABASE_URL", _url(db_engine))
    monkeypatch.setenv("WASSUP_PATIENTS_CLINIC", _slug(db_engine, seed.clinic_a))
    monkeypatch.setenv("WASSUP_PATIENTS_PATH", str(path))
    monkeypatch.setenv("WASSUP_PATIENTS_APPLY", "true")
    monkeypatch.delenv("WASSUP_ENVIRONMENT", raising=False)
    assert tool.main() == 2
    assert "WASSUP_PRODUCTION_ACK" in capsys.readouterr().err
    assert "P1" not in _patients(db_engine, seed.clinic_a)
    monkeypatch.setenv("WASSUP_ENVIRONMENT", "test")
    assert tool.main() == 0
    out = capsys.readouterr().out
    assert "committed" in out and "Test" not in out and "0412" not in out  # counts only
