"""Alembic environment. Migrations are forward-only in production.

Run as the ``wassup_migrator`` role (never a superuser). Each migration does ``SET ROLE
wassup_owner`` so every object is owned by the NOLOGIN owner role.

- **Transactional:** each migration runs in its own transaction together with its version stamp,
  so a failure part-way leaves the schema exactly at the previous revision (Postgres DDL is
  transactional). Never run this on an autocommit connection.
- **One migrator at a time:** a session advisory lock is taken *before* Alembic reads the current
  revision, so a second concurrent deploy waits and then finds nothing left to do.
- **Fail fast:** ``lock_timeout`` stops a migration queueing behind live traffic; the deploy fails
  and is retried instead of blocking the phone path.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

MIGRATION_LOCK_KEY = 727_001


def _sync_url(url: str) -> str:
    """Platforms hand out postgres:// URLs; the migrator uses the psycopg (v3) driver."""
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def _run(connection: Connection) -> None:
    if getattr(connection.connection.driver_connection, "autocommit", False):
        raise SystemExit("Migrations need a transactional connection, not AUTOCOMMIT")
    connection.execute(text("SET lock_timeout = '3s'"))
    connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
    connection.commit()  # Alembic must start each migration's transaction itself
    try:
        context.configure(
            connection=connection, target_metadata=None, transaction_per_migration=True
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if connection.in_transaction():
            connection.rollback()
        connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
        connection.commit()


def run_migrations_online() -> None:
    provided = context.config.attributes.get("connection")
    if provided is not None:  # tests pass an open connection
        _run(provided)
        return
    url = os.environ.get("WASSUP_MIGRATION_DATABASE_URL")
    if not url:
        raise SystemExit("WASSUP_MIGRATION_DATABASE_URL is not set")
    engine = create_engine(_sync_url(url))
    with engine.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported; run against a database.")
run_migrations_online()
