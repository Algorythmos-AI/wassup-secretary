"""Backups: every row of every clinic, encrypted, verified after upload; restore reproduces them
byte for byte; the schedule and health report behave."""

from __future__ import annotations

import importlib.util
import sys
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ops_worker import backup
from ops_worker.main import build_app
from sqlalchemy import text
from sqlalchemy.engine import Engine
from wassup_core.backups import BackupError, DirectoryStore, key_from_hex, read_archive

from tests.support.database import ROOT, Seed, migrated_database

pytestmark = pytest.mark.db

KEY = key_from_hex("42" * 32)


def _load_restore() -> ModuleType:
    spec = importlib.util.spec_from_file_location("restore", ROOT / "db" / "restore.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


restore = _load_restore()


def _plain(engine: Engine) -> str:
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


def _tables(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        names = conn.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
                "AND c.relname <> 'alembic_version'"
            )
        ).scalars()
        return {
            name: int(conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one())  # noqa: S608
            for name in names
        }


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory, db_engine: Engine, seed: Seed) -> Path:
    """One backup of the shared test database, taken as the backup role."""
    store = DirectoryStore(tmp_path_factory.mktemp("store"))
    summary = backup.backup_once(
        _plain(db_engine), KEY, store, "test", keep=5, set_role="wassup_backup"
    )
    assert summary["verified"] is True and summary["store"] == "directory"
    return store.root / str(summary["name"])


def test_backup_holds_every_table_and_every_clinic(
    archive: Path, db_engine: Engine, seed: Seed, tmp_path: Path
) -> None:
    manifest = read_archive(KEY, archive, tmp_path / "out")
    counts = _tables(db_engine)
    assert {t.name for t in manifest.tables} == set(counts)  # nothing skipped
    assert {t.name: t.rows for t in manifest.tables} == counts
    calls = (tmp_path / "out" / "tables" / "calls.tsv").read_text()
    assert str(seed.call_a) in calls and str(seed.call_b) in calls  # RLS bypassed: both clinics
    assert all(t.order_by for t in manifest.tables), "every table is keyed, so verifiable"
    with db_engine.connect() as conn:
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        sequences = {
            r.sequencename: r.last_value
            for r in conn.execute(
                text(
                    "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public'"
                )
            )
        }
    assert manifest.alembic_revision == revision
    assert {s.name: s.last_value for s in manifest.sequences} == sequences


def test_backup_refuses_a_role_that_does_not_bypass_rls(db_engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="bypasses row-level security"):
        backup.dump(_plain(db_engine), tmp_path / "t", "test", set_role="app_ops")
    with pytest.raises(BackupError, match="bypasses row-level security"):
        backup.dump(_plain(db_engine), tmp_path / "t", "test")  # the superuser itself


def test_backup_fails_when_the_stored_copy_differs(
    db_engine: Engine, seed: Seed, tmp_path: Path
) -> None:
    class LossyStore(DirectoryStore):
        def put(self, name: str, path: Path) -> None:
            data = bytearray(path.read_bytes())
            data[len(data) // 2] ^= 0x01
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_bytes(bytes(data))

    with pytest.raises(BackupError):
        backup.backup_once(
            _plain(db_engine), KEY, LossyStore(tmp_path / "s"), "test", set_role="wassup_backup"
        )


def test_restore_reproduces_every_row_and_refuses_bad_targets(
    archive: Path, db_engine: Engine, tmp_path: Path
) -> None:
    manifest = read_archive(KEY, archive, tmp_path / "unpacked")
    expected = _tables(db_engine)
    with migrated_database() as target:
        url = _plain(target)
        # Dry run: everything loaded and checked, then rolled back.
        counts = restore.run(url, manifest, tmp_path / "unpacked", apply=False)
        assert counts == expected
        assert sum(_tables(target).values()) == 0
        # Apply: the rows are there, byte for byte (checked by the tool against the manifest).
        assert restore.run(url, manifest, tmp_path / "unpacked", apply=True) == expected
        assert _tables(target) == expected
        with target.connect() as conn:
            seqs = {
                r.sequencename: r.last_value
                for r in conn.execute(
                    text(
                        "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public'"
                    )
                )
            }
        assert {s.name: s.last_value for s in manifest.sequences} == seqs
        # A second apply into the now non-empty target is refused unless told to wipe it.
        with pytest.raises(restore.RestoreRefused, match="not empty"):
            restore.run(url, manifest, tmp_path / "unpacked", apply=True)
        assert (
            restore.run(url, manifest, tmp_path / "unpacked", apply=True, truncate=True) == expected
        )
    with (
        migrated_database("0012") as older,
        pytest.raises(restore.RestoreRefused, match="revision"),
    ):
        restore.run(_plain(older), manifest, tmp_path / "unpacked", apply=True)


def test_restore_refuses_when_a_table_does_not_come_back_as_archived(
    archive: Path, tmp_path: Path
) -> None:
    manifest = read_archive(KEY, archive, tmp_path / "unpacked")
    clinics = tmp_path / "unpacked" / "tables" / "clinics.tsv"
    clinics.write_bytes(clinics.read_bytes().replace(b"NSW", b"VIC", 1))  # same rows, changed
    with migrated_database() as target:
        with pytest.raises(restore.RestoreRefused, match="differ"):
            restore.run(_plain(target), manifest, tmp_path / "unpacked", apply=True)
        assert sum(_tables(target).values()) == 0


def test_restore_cli_needs_the_production_acknowledgement(
    archive: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with migrated_database() as target:
        monkeypatch.setenv("WASSUP_ADMIN_DATABASE_URL", _plain(target))
        monkeypatch.setenv("WASSUP_BACKUP_KEY_HEX", "42" * 32)
        monkeypatch.setenv("WASSUP_RESTORE_PATH", str(archive))
        monkeypatch.setenv("WASSUP_RESTORE_APPLY", "true")
        monkeypatch.delenv("WASSUP_ENVIRONMENT", raising=False)
        assert restore.main() == 2
        assert "WASSUP_PRODUCTION_ACK" in capsys.readouterr().err
        assert sum(_tables(target).values()) == 0
        monkeypatch.setenv("WASSUP_PRODUCTION_ACK", target.url.database or "")
        assert restore.main() == 0
        out = capsys.readouterr().out
        assert "committed" in out and "verified=true" in out
        assert sum(_tables(target).values()) > 0
        # From the store by name, dry run, wrong key: refused before touching the database.
        monkeypatch.delenv("WASSUP_RESTORE_PATH")
        monkeypatch.setenv("WASSUP_BACKUP_DIR", str(archive.parent))
        monkeypatch.setenv("WASSUP_RESTORE_NAME", "latest")
        monkeypatch.setenv("WASSUP_BACKUP_KEY_HEX", "43" * 32)
        assert restore.main() == 1
        assert "wrong" in capsys.readouterr().err


# --- schedule and health ------------------------------------------------------------------------


def _cfg(**over: Any) -> backup.BackupConfig:
    base = {"environment": "test", "local_time": "03:30", "timezone": "Australia/Sydney"}
    return backup.BackupConfig(**{**base, **over})


def test_due_once_per_local_day_after_the_configured_time() -> None:
    cfg = _cfg()
    before = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)  # 13:00 Sydney, 25 Sep
    assert backup.due(cfg, None, datetime(2026, 9, 24, 17, 29, tzinfo=UTC)) is False  # 03:29
    assert backup.due(cfg, None, datetime(2026, 9, 24, 17, 30, tzinfo=UTC)) is True  # 03:30
    assert backup.due(cfg, date(2026, 9, 25), before) is False  # done today
    assert backup.due(cfg, date(2026, 9, 24), before) is True  # yesterday's; today is due


class _Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str]] = []

    async def send(self, to: list[str], subject: str, body: str) -> None:
        self.sent.append((to, subject))


async def test_tick_runs_when_due_alerts_on_failure_and_reports(tmp_path: Path) -> None:
    store = DirectoryStore(tmp_path / "store")
    monitor = backup.BackupMonitor(_cfg(ops_emails=["ops@example.test"]))
    sender = _Sender()
    ran: list[int] = []

    async def good() -> dict[str, Any]:
        ran.append(1)
        return {
            "name": "n",
            "bytes": 1,
            "tables": 2,
            "rows": 3,
            "store": "directory",
            "duration_s": 0.5,
        }

    async def bad() -> dict[str, Any]:
        raise BackupError("disk full")

    assert monitor.report()["status"] == "pending"
    not_yet = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)  # 03:00 Sydney
    assert await backup.tick(monitor, store, good, sender, not_yet) == "idle" and not ran
    due = datetime(2026, 9, 24, 17, 31, tzinfo=UTC)
    assert await backup.tick(monitor, store, bad, sender, due) == "failing"
    assert monitor.report()["status"] == "failing" and len(sender.sent) == 1
    assert await backup.tick(monitor, store, bad, sender, due) == "failing"
    assert len(sender.sent) == 1  # no re-alert within a day
    assert await backup.tick(monitor, store, good, sender, due) == "ok" and ran == [1]
    report = monitor.report()
    assert (
        report["status"] == "ok" and report["rows"] == 3 and monitor.last_day == date(2026, 9, 25)
    )
    assert await backup.tick(monitor, store, good, sender, due) == "idle"  # once per day
    # A nightly backup older than 36 hours is failing, whatever the last one said.
    assert monitor.report(time.time() + backup.STALE_AFTER_S + 1)["status"] == "failing"


def test_monitor_primes_itself_from_the_store(tmp_path: Path) -> None:
    store = DirectoryStore(tmp_path / "store")
    src = tmp_path / "x"
    src.write_bytes(b"x")
    made = datetime(2026, 9, 24, 17, 40, tzinfo=UTC)  # 03:40 Sydney on the 25th
    store.put(backup.archive_name("test", "0013", made), src)
    store.put(backup.archive_name("production", "0013", made), src)
    monitor = backup.BackupMonitor(_cfg())
    monitor.prime(store)
    assert monitor.last_day == date(2026, 9, 25)
    assert monitor.report(made.timestamp() + 3600)["status"] == "ok"
    assert backup.due(monitor.config, monitor.last_day, made) is False


def test_health_endpoint_reports_the_monitor() -> None:
    app = build_app()
    with TestClient(app) as client:
        assert client.get("/health/backup").status_code == 503
        assert client.get("/health/backup").json() == {"status": "unconfigured"}
        app.state.backup = backup.BackupMonitor(_cfg())
        assert client.get("/health/backup").status_code == 200
        app.state.backup.watch.record({"status": "failing", "reason": "BackupError"})
        assert client.get("/health/backup").status_code == 503


def test_stale_monitor_stays_honest_after_a_failure() -> None:
    monitor = backup.BackupMonitor(_cfg())
    monitor.watch.record({"status": "ok", "last_backup_at": "t"})
    monitor.last_day = date(2026, 9, 25)
    good_at = monitor.watch.last_success
    monitor.watch.record({"status": "failing", "reason": "x"})
    assert monitor.report()["status"] == "failing"
    assert good_at is not None and uuid  # keep imports honest
