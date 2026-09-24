"""Alembic environment. Migrations are forward-only in production.

Run as the ``wassup_migrator`` role (never a superuser). Each migration does ``SET ROLE
wassup_owner`` so every object is owned by the NOLOGIN owner role. A session advisory lock
makes concurrent deploys safe, and a short ``lock_timeout`` keeps a migration from queueing
behind live traffic (it fails fast and the deploy retries).
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

MIGRATION_LOCK_KEY = 727_001


def _run(connection: Connection) -> None:
    connection.execute(text("SET lock_timeout = '3s'"))
    connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
    try:
        context.configure(
            connection=connection, target_metadata=None, transaction_per_migration=True
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})


def run_migrations_online() -> None:
    provided = context.config.attributes.get("connection")
    if provided is not None:  # tests pass an open connection
        _run(provided)
        return
    url = os.environ.get("WASSUP_MIGRATION_DATABASE_URL")
    if not url:
        raise SystemExit("WASSUP_MIGRATION_DATABASE_URL is not set")
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported; run against a database.")
run_migrations_online()
