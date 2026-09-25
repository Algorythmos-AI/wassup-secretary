"""Restore an encrypted backup archive into THIS database (run inside the platform as db-admin).

The target must be a database migrated to the same schema revision the archive was taken at
and, unless told to wipe it, empty. Everything happens in one transaction as the database
admin with foreign-key and trigger firing suspended (the snapshot was consistent when taken),
then every table's row count — and, for keyed tables, the exact bytes of its rows — is compared
with the manifest before commit. A dry run does all of that and rolls back.

    WASSUP_ADMIN_DATABASE_URL   admin (superuser) URL of the TARGET database
    WASSUP_BACKUP_KEY_HEX       the key the archive was made with
    WASSUP_RESTORE_PATH         a local archive file, or
    WASSUP_RESTORE_NAME         an archive in the store (WASSUP_BACKUP_S3_* / WASSUP_BACKUP_DIR),
                                or "latest" (the newest from WASSUP_RESTORE_SOURCE)
    WASSUP_RESTORE_SOURCE       the environment the archive must come from (required: a staging
                                archive is never restored by mistake where production was meant)
                                or "latest"
    WASSUP_RESTORE_APPLY        "true" to commit; otherwise a dry run
    WASSUP_RESTORE_TRUNCATE     "true" to empty every table in the archive first (destructive)
    WASSUP_PRODUCTION_ACK       in production, an apply or a truncate (even in a dry run, which
                                locks every table while it runs) needs this set to the database name

Output is table names and counts only.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import psycopg
from ops_worker.backup import copy_out, pin_session
from psycopg import sql
from wassup_core.backups import (
    BackupError,
    Manifest,
    key_from_hex,
    latest_archive,
    read_archive,
    store_from_env,
    table_member,
)


class RestoreRefused(Exception):
    pass


class _DryRun(Exception):
    pass


def _url(raw: str) -> str:
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix) :]
    return raw


def _columns(conn: psycopg.Connection[Any], table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT a.attname FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        WHERE c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = ''
        ORDER BY a.attnum
        """,
        (table,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _copy_out_sha(
    conn: psycopg.Connection[Any], table: str, columns: list[str], order_by: list[str]
) -> tuple[str, int]:
    """The table as the backup wrote it (same query, same pinned settings), hashed in a stream."""
    sha, rows = hashlib.sha256(), 0
    with conn.cursor().copy(copy_out(table, columns, order_by)) as copy:
        for chunk in copy:
            data = bytes(chunk)
            sha.update(data)
            rows += data.count(b"\n")
    return sha.hexdigest(), rows


def _refuse_unless_compatible(
    conn: psycopg.Connection[Any], manifest: Manifest, *, truncate: bool
) -> list[str]:
    """Same schema revision, every archived table and column present; returns the tables that
    already hold rows (refused unless the caller asked for them to be wiped)."""
    revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    if revision is None or str(revision[0]) != manifest.alembic_revision:
        raise RestoreRefused(
            f"target is at schema revision {revision[0] if revision else None}, the archive at "
            f"{manifest.alembic_revision}: migrate the target to exactly that revision first"
        )
    for table in manifest.tables:
        present = _columns(conn, table.name)
        if not present:
            raise RestoreRefused(f"target has no table {table.name}")
        if set(table.columns) - set(present):
            raise RestoreRefused(f"target table {table.name} lacks archived columns")
    non_empty = [
        t.name
        for t in manifest.tables
        if conn.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(sql.Identifier("public", t.name))
        ).fetchone()[0]  # type: ignore[index]
    ]
    if non_empty and not truncate:
        raise RestoreRefused(
            f"target is not empty ({len(non_empty)} tables have rows); "
            "set WASSUP_RESTORE_TRUNCATE=true to wipe them first"
        )
    return non_empty


def _load(conn: psycopg.Connection[Any], manifest: Manifest, tables_dir: Path) -> None:
    for table in manifest.tables:
        query = sql.SQL("COPY {} ({}) FROM STDIN").format(
            sql.Identifier("public", table.name),
            sql.SQL(", ").join(sql.Identifier(c) for c in table.columns),
        )
        path = tables_dir / table_member(table.name)
        with path.open("rb") as inp, conn.cursor().copy(query) as copy:
            while chunk := inp.read(1024 * 1024):
                copy.write(chunk)
    for seq in manifest.sequences:
        if seq.last_value is not None:
            conn.execute("SELECT setval(%s, %s, true)", (f'public."{seq.name}"', seq.last_value))


def _verify(conn: psycopg.Connection[Any], manifest: Manifest) -> dict[str, int]:
    counts: dict[str, int] = {}
    problems: list[str] = []
    for table in manifest.tables:
        sha, rows = _copy_out_sha(conn, table.name, table.columns, table.order_by)
        counts[table.name] = rows
        if rows != table.rows or (table.order_by and sha != table.sha256):
            problems.append(table.name)
    if problems:
        raise RestoreRefused(
            f"after loading, {len(problems)} tables differ from the archive "
            f"({', '.join(problems[:5])}); nothing kept"
        )
    return counts


def run(
    admin_url: str,
    manifest: Manifest,
    tables_dir: Path,
    *,
    apply: bool,
    truncate: bool = False,
) -> dict[str, int]:
    """Restore ``manifest``'s tables (files in ``tables_dir``) into the database at ``admin_url``.
    Returns per-table row counts after the restore (they equal the manifest's, or it raised)."""
    counts: dict[str, int] = {}
    with psycopg.connect(_url(admin_url)) as conn:
        try:
            with conn.transaction():
                conn.execute("SET LOCAL lock_timeout = '10s'")
                pin_session(conn)  # read and re-written exactly as the backup wrote them
                non_empty = _refuse_unless_compatible(conn, manifest, truncate=truncate)
                # The snapshot was consistent: load without re-checking keys or firing triggers
                # (the audit chain heads are restored as data, so the chain stays verifiable).
                conn.execute("SET LOCAL session_replication_role = replica")
                if non_empty:
                    conn.execute(
                        sql.SQL("TRUNCATE {}").format(
                            sql.SQL(", ").join(
                                sql.Identifier("public", t.name) for t in manifest.tables
                            )
                        )
                    )
                _load(conn, manifest, tables_dir)
                counts = _verify(conn, manifest)
                if not apply:
                    raise _DryRun
        except _DryRun:
            pass
    return counts


def main() -> int:
    env = os.environ
    admin_url = env.get("WASSUP_ADMIN_DATABASE_URL", "")
    key_hex = env.get("WASSUP_BACKUP_KEY_HEX", "")
    if not admin_url or not key_hex:
        print("WASSUP_ADMIN_DATABASE_URL and WASSUP_BACKUP_KEY_HEX are required", file=sys.stderr)
        return 2
    apply = env.get("WASSUP_RESTORE_APPLY") == "true"
    truncate = env.get("WASSUP_RESTORE_TRUNCATE") == "true"
    production = env.get("WASSUP_ENVIRONMENT", "") not in ("local", "test", "staging")
    source = env.get("WASSUP_RESTORE_SOURCE", "").strip()
    if not source:
        print("WASSUP_RESTORE_SOURCE (the archive's environment) is required", file=sys.stderr)
        return 2
    try:
        key = key_from_hex(key_hex)
        with tempfile.TemporaryDirectory(prefix="wassup-restore-") as tmp:
            work = Path(tmp)
            archive = _fetch(env, work, source)
            manifest = read_archive(key, archive, work / "unpacked")
            if manifest.environment != source:
                raise RestoreRefused(
                    f"the archive is from {manifest.environment!r}, not {source!r}"
                )
            print(
                f"archive={archive.name} archive_environment={manifest.environment} "
                f"revision={manifest.alembic_revision} tables={len(manifest.tables)} "
                f"rows={manifest.total_rows} verified=true"
            )
            if (apply or truncate) and production:
                with psycopg.connect(_url(admin_url)) as conn:
                    dbname = conn.execute("SELECT current_database()").fetchone()[0]  # type: ignore[index]
                if env.get("WASSUP_PRODUCTION_ACK") != dbname:
                    print(
                        "restore refused: applying or truncating in production needs "
                        f"WASSUP_PRODUCTION_ACK set to the target database name ({dbname!r})",
                        file=sys.stderr,
                    )
                    return 2
            counts = run(admin_url, manifest, work / "unpacked", apply=apply, truncate=truncate)
    except (BackupError, RestoreRefused, OSError) as exc:
        print(f"restore refused: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:  # never echo a database message: it can quote a row
        print(f"restore refused: database error {type(exc).__name__}", file=sys.stderr)
        return 1
    print(" ".join(f"{name}={rows}" for name, rows in counts.items()))
    print("committed" if apply else "dry run: loaded and verified, then rolled back")
    return 0


def _fetch(env: os._Environ[str], work: Path, source: str) -> Path:
    path = env.get("WASSUP_RESTORE_PATH", "")
    if path:
        return Path(path)
    name = env.get("WASSUP_RESTORE_NAME", "")
    if not name:
        raise BackupError("WASSUP_RESTORE_PATH or WASSUP_RESTORE_NAME is required")
    store = store_from_env(env)
    if store is None:
        raise BackupError(
            "WASSUP_RESTORE_NAME needs a store (WASSUP_BACKUP_S3_* or WASSUP_BACKUP_DIR)"
        )
    if name == "latest":
        newest = latest_archive(store, source)
        if newest is None:
            raise BackupError(f"the store holds no {source} archives")
        name = newest
    dest = work / name
    store.get(name, dest)
    return dest


if __name__ == "__main__":
    raise SystemExit(main())
