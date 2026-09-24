"""Tenancy test harness: a throwaway database, migrated exactly as production is.

Needs TEST_DATABASE_ADMIN_URL (a superuser URL on a disposable Postgres — CI provides one).
The harness creates a fresh database, applies ``db/roles.sql`` and ``db/grant_database.sql``,
then runs Alembic *as the non-superuser migrator* (SET SESSION AUTHORIZATION), so the tests
prove the real permission model rather than a superuser shortcut. Only synthetic data is used.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.engine import Connection, Engine

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL")

pytestmark = pytest.mark.db


@dataclass(frozen=True)
class Seed:
    clinic_a: uuid.UUID
    clinic_b: uuid.UUID
    call_a: uuid.UUID
    call_b: uuid.UUID


def _sqlalchemy_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+psycopg://", 1).replace(
        "postgres://", "postgresql+psycopg://", 1
    )


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    if not ADMIN_URL:
        pytest.skip("TEST_DATABASE_ADMIN_URL not set (tenancy tests need a disposable Postgres)")
    admin_url = make_url(_sqlalchemy_url(ADMIN_URL))
    db_name = f"wassup_t_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE \"{db_name}\" ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0"
            )
        )
    setup_engine = create_engine(admin_url.set(database=db_name), isolation_level="AUTOCOMMIT")
    # Tests need real transactions: SET LOCAL ROLE / set_config(..., true) last until COMMIT.
    # Under autocommit they would expire after one statement and silently run as superuser.
    test_engine = create_engine(admin_url.set(database=db_name))
    try:
        with setup_engine.connect() as conn:
            # Raw SQL files go straight to the driver with no parameters, so '%' in format()
            # strings is not mistaken for a placeholder.
            raw = conn.connection.driver_connection
            raw.execute((ROOT / "db" / "roles.sql").read_text())
            grants = (
                (ROOT / "db" / "grant_database.sql")
                .read_text()
                .replace(':"dbname"', f'"{db_name}"')
            )
            raw.execute(grants)
            conn.execute(text("SET SESSION AUTHORIZATION wassup_migrator"))
            cfg = Config(str(ROOT / "db" / "alembic.ini"))
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            conn.execute(text("RESET SESSION AUTHORIZATION"))
        yield test_engine
    finally:
        test_engine.dispose()
        setup_engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin.dispose()


def as_role(conn: Connection, role: str, clinics: list[uuid.UUID] | None) -> None:
    """Act as an app role inside the current transaction, with an optional clinic context."""
    assert conn.in_transaction(), "as_role needs an open transaction"
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    assert conn.execute(text("SELECT current_user")).scalar() == role
    if clinics is not None:
        literal = "{" + ",".join(str(c) for c in clinics) + "}"
        conn.execute(text("SELECT set_config('app.clinic_ids', :v, true)"), {"v": literal})


@pytest.fixture(scope="session")
def seed(engine: Engine) -> Seed:
    """Two synthetic clinics with one call each, written by the owner under an explicit context."""
    a, b = uuid.uuid4(), uuid.uuid4()
    call_a, call_b = uuid.uuid4(), uuid.uuid4()
    with engine.connect() as conn, conn.begin():
        org = conn.execute(
            text("INSERT INTO organizations (name) VALUES ('Test Org') RETURNING id")
        ).scalar_one()
        as_role(conn, "wassup_owner", [a, b])
        for clinic, slug, call in ((a, "test-clinic-a", call_a), (b, "test-clinic-b", call_b)):
            conn.execute(
                text(
                    "INSERT INTO clinics (id, organization_id, slug, name, state) "
                    "VALUES (:id, :org, :slug, :slug, 'NSW')"
                ),
                {"id": clinic, "org": org, "slug": slug},
            )
            conn.execute(
                text(
                    "INSERT INTO calls (id, clinic_id, provider_call_id, direction) "
                    "VALUES (:id, :c, :pc, 'inbound')"
                ),
                {"id": call, "c": clinic, "pc": f"call_{slug}"},
            )
        conn.execute(
            text(
                "INSERT INTO clinic_voice_agents (clinic_id, agent_id, environment) "
                "VALUES (:a, 'agent_test_a', 'staging'), (:b, 'agent_test_b', 'staging')"
            ),
            {"a": a, "b": b},
        )
        conn.execute(
            text(
                "INSERT INTO clinic_phone_numbers (clinic_id, e164) "
                "VALUES (:a, '+61400000101'), (:b, '+61400000102')"
            ),
            {"a": a, "b": b},
        )
    return Seed(a, b, call_a, call_b)
