"""Nightly encrypted backup of the whole database, verified after upload.

Every table is copied out in one REPEATABLE READ snapshot as ``wassup_backup`` — the read-only
role that bypasses row-level security — packed with a manifest (schema revision, per-table row
counts and SHA-256s, sequence positions), encrypted (``wassup_core.backups``) and put in the
configured store (an S3-compatible bucket, or a directory). The archive is then fetched back
from the store and fully decrypted and checked, so ``verified`` means the stored bytes restore
to exactly what was dumped. ``db/restore.py`` is the other half; the drill is in
``docs/runbooks/restore-drill.md``.

Two ways to run it:

- **in ops-worker** (``run`` mode, when ops-worker has the backup role's URL and the key): the job
  ticks every minute and backs up once per local day after the configured time;
- **as a one-shot** ``WASSUP_ROLE=backup`` on a schedule (a cron service), which keeps the
  all-clinics credential and the key out of the long-running worker. ops-worker then only
  **watches** the store (``watch`` mode: store settings, no database URL or key) and reports
  whether a fresh archive exists.

Either way the external heartbeat is pinged only after a verified backup, so a missed night
pages someone. An upload is written under a pending name and renamed only once the stored copy
has been fetched back and verified, so nothing unverified ever looks like a backup.
"""

from __future__ import annotations

import asyncio
import os
import shutil
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

import httpx
import psycopg
from psycopg import sql
from wassup_core.backups import (
    BackupError,
    Manifest,
    SequenceEntry,
    Store,
    TableEntry,
    archive_name,
    archives_of,
    digest_file,
    key_from_hex,
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
RETRY_AFTER_S = 3600  # after a failure, try again in an hour, not every minute
WATCH_INTERVAL_S = 900.0
PENDING_SUFFIX = ".pending"
_TMP_PREFIX = "wassup-backup-"

# Every dump and every restore check runs with the same output settings, so the bytes of a
# table do not depend on the server's or the database's defaults (time zone above all).
_PIN_SESSION = (
    "SET LOCAL TimeZone = 'UTC'",
    "SET LOCAL DateStyle = 'ISO, YMD'",
    "SET LOCAL IntervalStyle = 'postgres'",
    "SET LOCAL extra_float_digits = 3",
    "SET LOCAL bytea_output = 'hex'",
)

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


def pin_session(conn: psycopg.Connection[Any]) -> None:
    """Inside a transaction: fix every setting that changes how values are written as text."""
    for statement in _PIN_SESSION:
        conn.execute(statement)


def copy_out(name: str, columns: list[str], order_by: list[str]) -> sql.Composed:
    """``COPY (SELECT cols FROM table ORDER BY key) TO STDOUT``. Rows are ordered by the text of
    the primary key under the C collation, so the order is the same on any server whatever its
    locale; a restore recomputes exactly this and compares the bytes."""
    order = sql.SQL(", ").join(
        sql.SQL('{}::text COLLATE "C"').format(sql.Identifier(c)) for c in order_by
    )
    return sql.SQL("COPY (SELECT {} FROM {}{}) TO STDOUT").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.Identifier("public", name),
        sql.SQL(" ORDER BY {}").format(order) if order_by else sql.SQL(""),
    )


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
            pin_session(conn)
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


def _clear_stale_workdirs(max_age_s: float = 6 * 3600) -> None:
    """A killed run leaves plaintext table files behind in its work directory: remove any older
    than a backup could take."""
    root = Path(tempfile.gettempdir())
    for path in root.glob(f"{_TMP_PREFIX}*"):
        try:
            if path.is_dir() and time.time() - path.stat().st_mtime > max_age_s:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def backup_once(
    url: str,
    key: bytes,
    store: Store,
    environment: str,
    *,
    keep: int = 30,
    set_role: str | None = None,
) -> dict[str, Any]:
    """Dump, encrypt, upload under a pending name, fetch back and verify, then publish under the
    archive's real name and prune. Blocking. Nothing unverified is ever left under a real name."""
    started = time.monotonic()
    _clear_stale_workdirs()
    with tempfile.TemporaryDirectory(prefix=_TMP_PREFIX) as tmp:
        work = Path(tmp)
        manifest = dump(url, work / "tables", environment, set_role=set_role)
        name = archive_name(environment, manifest.alembic_revision)
        pending = name + PENDING_SUFFIX
        archive = work / name
        size = write_archive(key, archive, manifest, work / "tables")
        shutil.rmtree(work / "tables")  # the plaintext is no longer needed
        try:
            store.put(pending, archive)
            fetched = work / "fetched.wsb"
            store.get(pending, fetched)
            if fetched.stat().st_size != size:
                raise BackupError("stored archive size differs from what was uploaded")
            if verify_archive(key, fetched) != manifest:
                raise BackupError("stored archive does not match what was dumped")
            store.rename(pending, name)
        except BaseException:
            try:
                store.delete(pending)
            except Exception as exc:  # the original failure is what matters
                log.warning("backup_pending_cleanup_failed", code=type(exc).__name__)
            raise
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
    # "run": this process backs up; "watch": another process does, this one checks the store.
    mode: str = "run"

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
    retry_after: float = 0.0
    watch_alerted_at: float = 0.0
    # Set when backups are asked for but can't work (bad key, half-set bucket, …).
    config_error: str | None = None

    def report(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        if self.config_error:
            return {"status": "failing", "reason": self.config_error, "mode": self.config.mode}
        if self.watch.latest is None:
            # Nothing yet: not failing until a whole cycle has passed since the worker started.
            stale = now - self.started_at > self.watch.stale_after_s
            return {"status": "failing" if stale else "pending", "reason": "no_backup_yet"}
        return {**self.watch.report(now), "mode": self.config.mode}

    def prime(self, store: Store) -> None:
        """Learn the newest finished archive in the store: a restart then neither repeats last
        night's backup nor forgets it happened; in watch mode this is the whole check."""
        found = archives_of(store, self.config.environment)
        if found:
            created, name = found[-1]
            self.last_day = created.astimezone(self.config.zone).date()
            self.watch.latest = {
                "status": "ok",
                "name": name,
                "last_backup_at": created.isoformat(),
                "store": store.kind,
            }
            self.watch.last_success = created.timestamp()
        self.primed = True

    def failed(self, reason: str, store_kind: str, now: float) -> bool:
        """Record a failure; returns whether ops should be emailed now. The last good backup's
        time is kept, so staleness is measured from it, not from the failure."""
        last_good = self.watch.last_success
        alert_due, _ = self.watch.record(
            {"status": "failing", "reason": reason, "store": store_kind}
        )
        self.watch.last_success = last_good
        self.retry_after = now + RETRY_AFTER_S
        return alert_due


def due(config: BackupConfig, last_day: date | None, now: datetime | None = None) -> bool:
    local = (now or datetime.now(UTC)).astimezone(config.zone)
    return local.date() != last_day and local.time() >= config.run_after


async def tick(
    monitor: BackupMonitor,
    store: Store,
    run: Callable[[], Awaitable[dict[str, Any]]] | None,
    email: EmailSender,
    now: datetime | None = None,
) -> str:
    """Scheduled job. Run mode: prime once, back up when due, remember the outcome, alert on a
    failure and retry an hour later. Watch mode (``run`` is None): re-read the store."""
    started = now or datetime.now(UTC)
    now_ts = started.timestamp()
    try:
        if run is None or not monitor.primed:
            await asyncio.to_thread(monitor.prime, store)
    except Exception as exc:  # an unreachable store is an incident too
        return await _failure(monitor, email, f"store_{type(exc).__name__}", store.kind, now_ts)
    if run is None:
        status = str(monitor.report(now_ts)["status"])
        if status == "failing" and now_ts - monitor.watch_alerted_at > REALERT_EVERY_S:
            monitor.watch_alerted_at = now_ts
            log.error("backup_stale", code="no_recent_archive")
            await _alert(email, monitor.config.ops_emails, {"reason": "no_recent_archive"})
        return status
    if now_ts < monitor.retry_after:
        return "idle"
    first_run = monitor.config.on_start and monitor.watch.latest is None
    if not first_run and not due(monitor.config, monitor.last_day, started):
        return "idle"
    try:
        summary = await run()
    except Exception as exc:  # a failed backup is an incident, not a crash
        return await _failure(monitor, email, type(exc).__name__, store.kind, now_ts)
    monitor.last_day = started.astimezone(monitor.config.zone).date()
    monitor.retry_after = 0.0
    monitor.watch.record({"status": "ok", "last_backup_at": started.isoformat(), **summary})
    log.info(
        "backup_done", count=int(summary["rows"]), duration_ms=int(summary["duration_s"] * 1000)
    )
    await ping(monitor.config.heartbeat_url)
    return "ok"


async def _failure(
    monitor: BackupMonitor, email: EmailSender, reason: str, store_kind: str, now_ts: float
) -> str:
    log.error("backup_failed", code=reason)
    if monitor.failed(reason, store_kind, now_ts):
        await _alert(email, monitor.config.ops_emails, {"reason": reason})
    return "failing"


async def _alert(email: EmailSender, ops_emails: list[str], report: dict[str, Any]) -> None:
    if not ops_emails:
        return
    body = "\n".join(
        [
            "The database backup FAILED, so the newest restorable copy is an older one.",
            f"Reason: {report.get('reason', 'unknown')}",
            "",
            "Check ops-worker's /health/backup and its logs (event backup_failed). It retries in",
            "an hour; a backup by hand is described in docs/runbooks/restore-drill.md.",
        ]
    )
    try:
        await email.send(ops_emails, "[WASSUP] backup failed", body)
    except Exception as exc:  # the alert path must not take the job down
        log.error("backup_alert_failed", code=type(exc).__name__)


def main() -> int:
    """``WASSUP_ROLE=backup``: one backup now, then exit (a cron service, a drill, or by hand).
    Pings ``WASSUP_BACKUP_HEARTBEAT_URL`` after a verified backup."""
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
    except (BackupError, OSError, ValueError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:  # never echo a database message: it can quote a row
        print(f"backup failed: database error {type(exc).__name__}", file=sys.stderr)
        return 1
    except Exception as exc:  # e.g. the bucket client: its message is ours to print, not data
        print(f"backup failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(" ".join(f"{k}={v}" for k, v in summary.items()))
    heartbeat = env.get("WASSUP_BACKUP_HEARTBEAT_URL", "")
    if heartbeat:
        try:
            httpx.get(heartbeat, timeout=5.0)
        except httpx.HTTPError:
            print("heartbeat failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
