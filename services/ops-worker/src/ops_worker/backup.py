"""Nightly encrypted backup of the whole database, verified after upload.

Every table is copied out in one REPEATABLE READ snapshot as ``wassup_backup`` — the read-only
role that bypasses row-level security — packed with a manifest (schema revision, per-table row
counts and SHA-256s, sequence positions), encrypted (``wassup_core.backups``) and put in the
configured store (an S3-compatible bucket, or a directory). The archive is then fetched back
from the store and fully decrypted and checked, so ``verified`` means the stored bytes restore
to exactly what was dumped. ``db/restore.py`` is the other half; the drill is in
``docs/runbooks/restore-drill.md``.

The job ticks every minute and runs once per local day after the configured time; the external
heartbeat is pinged only after a verified backup, so a missed night pages someone.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from wassup_core.backups import (
    BackupError,
    Manifest,
    SequenceEntry,
    Store,
    TableEntry,
    archive_name,
    digest_file,
    key_from_hex,
    parse_archive_name,
    prune,
    store_from_env,
    verify_archive,
    write_archive,
)
from wassup_core.logging import get_logger

from ops_worker.notifier import EmailSender
from ops_worker.scheduler import ping
from ops_worker.watch import Watch

log = get_logger(__name__)

STALE_AFTER_S = 36 * 3600  # a nightly backup older than this is an incident
REALERT_EVERY_S = 24 * 3600

_TABLES = """
SELECT c.relname AS name,
       array_agg(a.attname::text ORDER BY a.attnum) AS columns,
       COALESCE((SELECT array_agg(k.attname::text ORDER BY ord)
                 FROM pg_index i
                 CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS u(attnum, ord)
                 JOIN pg_attribute k ON k.attrelid = c.oid AND k.attnum = u.attnum
                 WHERE i.indrelid = c.oid AND i.indisprimary), ARRAY[]::text[]) AS order_by
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                   AND a.attgenerated = ''
WHERE c.relkind IN ('r', 'p') AND c.relname <> 'alembic_version'
  AND NOT EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid)
GROUP BY c.oid, c.relname
ORDER BY c.relname
"""
_SEQUENCES = """
SELECT sequencename AS name, last_value FROM pg_sequences WHERE schemaname = 'public'
ORDER BY sequencename
"""
_WHO = """
SELECT current_user AS who, r.rolbypassrls, r.rolsuper, current_database() AS db
FROM pg_roles r WHERE r.rolname = current_user
"""


def _plain_url(raw: str) -> str:
    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://", "postgres://"):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix) :]
    return raw


def copy_out(name: str, columns: list[str], order_by: list[str]) -> sql.Composed:
    """``COPY (SELECT cols FROM table ORDER BY key) TO STDOUT``: deterministic when keyed, so a
    restore can be checked byte for byte; also what a restore recomputes."""
    query = sql.SQL("COPY (SELECT {} FROM {}{}) TO STDOUT").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.Identifier("public", name),
        sql.SQL(" ORDER BY {}").format(sql.SQL(", ").join(sql.Identifier(c) for c in order_by))
        if order_by
        else sql.SQL(""),
    )
    return query


def dump(url: str, tables_dir: Path, environment: str, *, set_role: str | None = None) -> Manifest:
    """Copy every table out in one consistent read-only snapshot. Blocking; run in a thread."""
    tables_dir.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(_plain_url(url), autocommit=True) as conn:
        if set_role:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(set_role)))
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.transaction():
            who = conn.execute(_WHO).fetchone()
            if who is None or who[2] or not who[1]:
                raise BackupError(
                    "backups run as a non-superuser role that bypasses row-level security "
                    "(wassup_backup); anything else would silently miss rows"
                )
            revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            if revision is None:
                raise BackupError("database has no schema revision")
            entries: list[TableEntry] = []
            for name, columns, order_by in conn.execute(_TABLES).fetchall():
                path = tables_dir / f"{name}.tsv"
                with (
                    path.open("wb") as out,
                    conn.cursor().copy(copy_out(name, columns, order_by)) as copy,
                ):
                    for chunk in copy:
                        out.write(chunk)
                sha, size, rows = digest_file(path)
                entries.append(
                    TableEntry(
                        name=name,
                        columns=list(columns),
                        rows=rows,
                        sha256=sha,
                        bytes=size,
                        order_by=list(order_by),
                    )
                )
            sequences = [
                SequenceEntry(name=name, last_value=None if last is None else int(last))
                for name, last in conn.execute(_SEQUENCES).fetchall()
            ]
            return Manifest(
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                environment=environment,
                database=str(who[3]),
                alembic_revision=str(revision[0]),
                tables=entries,
                sequences=sequences,
            )


def backup_once(
    url: str,
    key: bytes,
    store: Store,
    environment: str,
    *,
    keep: int = 30,
    set_role: str | None = None,
) -> dict[str, Any]:
    """Dump, encrypt, upload, fetch back and verify, then prune old archives. Blocking."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="wassup-backup-") as tmp:
        work = Path(tmp)
        manifest = dump(url, work / "tables", environment, set_role=set_role)
        name = archive_name(environment, manifest.alembic_revision)
        archive = work / name
        size = write_archive(key, archive, manifest, work / "tables")
        store.put(name, archive)
        fetched = work / "fetched.wsb"
        store.get(name, fetched)
        if fetched.stat().st_size != size:
            raise BackupError("stored archive size differs from what was uploaded")
        stored = verify_archive(key, fetched)
        if stored != manifest:
            raise BackupError("stored archive does not match what was dumped")
        pruned = prune(store, environment, keep)
    return {
        "name": name,
        "bytes": size,
        "tables": len(manifest.tables),
        "rows": manifest.total_rows,
        "revision": manifest.alembic_revision,
        "store": store.kind,
        "verified": True,
        "pruned": len(pruned),
        "duration_s": round(time.monotonic() - started, 1),
    }


# --- scheduling and health ----------------------------------------------------------------------


@dataclass(frozen=True)
class BackupConfig:
    environment: str
    local_time: str = "03:30"
    timezone: str = "Australia/Sydney"
    keep: int = 30
    ops_emails: list[str] = field(default_factory=list)
    heartbeat_url: str = ""
    on_start: bool = False

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def run_after(self) -> dtime:
        hour, minute = self.local_time.split(":", 1)
        return dtime(int(hour), int(minute))


@dataclass
class BackupMonitor:
    config: BackupConfig
    watch: Watch = field(default_factory=lambda: Watch(STALE_AFTER_S, REALERT_EVERY_S))
    last_day: date | None = None
    primed: bool = False
    started_at: float = field(default_factory=time.time)

    def report(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        if self.watch.latest is None:
            # Nothing yet: not failing until a whole cycle has passed since the worker started.
            stale = now - self.started_at > self.watch.stale_after_s
            return {"status": "failing" if stale else "pending", "reason": "no_backup_yet"}
        return self.watch.report(now)

    def prime(self, store: Store) -> None:
        """Learn the latest archive already in the store, so a restart neither repeats last
        night's backup nor forgets that it happened."""
        newest = None
        for name in store.list():
            parsed = parse_archive_name(name)
            if parsed and parsed[0] == self.config.environment:
                newest = (parsed[1], name)
        if newest:
            created, name = newest
            self.last_day = created.astimezone(self.config.zone).date()
            self.watch.latest = {
                "status": "ok",
                "name": name,
                "last_backup_at": created.isoformat(),
                "verified": None,  # verified when it was made; not re-checked here
                "store": store.kind,
            }
            self.watch.last_success = created.timestamp()
        self.primed = True


def due(config: BackupConfig, last_day: date | None, now: datetime | None = None) -> bool:
    local = (now or datetime.now(UTC)).astimezone(config.zone)
    return local.date() != last_day and local.time() >= config.run_after


async def tick(
    monitor: BackupMonitor,
    store: Store,
    run: Callable[[], Awaitable[dict[str, Any]]],
    email: EmailSender,
    now: datetime | None = None,
) -> str:
    """Scheduled every minute: prime once, run when due, remember the outcome, alert on failure."""
    if not monitor.primed:
        await asyncio.to_thread(monitor.prime, store)
    if not (monitor.config.on_start and monitor.watch.latest is None) and not due(
        monitor.config, monitor.last_day, now
    ):
        return "idle"
    started = now or datetime.now(UTC)
    try:
        summary = await run()
    except Exception as exc:  # a failed backup is an incident, not a crash
        report = {"status": "failing", "reason": type(exc).__name__, "store": store.kind}
        alert_due, _ = monitor.watch.record(report)
        # record() counts a failure as a "success" of the check; keep the last good time honest.
        monitor.watch.last_success = monitor.watch.last_success if monitor.last_day else None
        log.error("backup_failed", code=type(exc).__name__)
        if alert_due:
            await _alert(email, monitor.config.ops_emails, report)
        return "failing"
    monitor.last_day = started.astimezone(monitor.config.zone).date()
    monitor.watch.record({"status": "ok", "last_backup_at": started.isoformat(), **summary})
    log.info(
        "backup_done", count=int(summary["rows"]), duration_ms=int(summary["duration_s"] * 1000)
    )
    await ping(monitor.config.heartbeat_url)
    return "ok"


async def _alert(email: EmailSender, ops_emails: list[str], report: dict[str, Any]) -> None:
    if not ops_emails:
        return
    body = "\n".join(
        [
            "Tonight's database backup FAILED, so the newest restorable copy is yesterday's.",
            f"Reason: {report.get('reason', 'unknown')}",
            "",
            "Check ops-worker's /health/backup and its logs (event backup_failed), then run a",
            "backup by hand: docs/runbooks/restore-drill.md.",
        ]
    )
    try:
        await email.send(ops_emails, "[WASSUP] backup failed", body)
    except Exception as exc:  # the alert path must not take the job down
        log.error("backup_alert_failed", code=type(exc).__name__)


def main() -> int:
    """``WASSUP_ROLE=backup``: one backup now, then exit (used by the drill and by hand)."""
    env = os.environ
    url = env.get("WASSUP_BACKUP_DATABASE_URL", "")
    key_hex = env.get("WASSUP_BACKUP_KEY_HEX", "")
    environment = env.get("WASSUP_ENVIRONMENT", "production")
    try:
        store = store_from_env(env)
        if not (url and key_hex and store):
            raise BackupError(
                "WASSUP_BACKUP_DATABASE_URL, WASSUP_BACKUP_KEY_HEX and a store "
                "(WASSUP_BACKUP_S3_* or WASSUP_BACKUP_DIR) are required"
            )
        summary = backup_once(
            url,
            key_from_hex(key_hex),
            store,
            environment,
            keep=int(env.get("WASSUP_BACKUP_KEEP", "30")),
        )
    except (BackupError, psycopg.Error, OSError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(" ".join(f"{k}={v}" for k, v in summary.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
