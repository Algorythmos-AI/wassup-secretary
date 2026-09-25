"""db/bootstrap.py: roles and passwords on a fresh database, idempotently, never echoing secrets."""

from __future__ import annotations

import importlib.util
import secrets
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.engine import Engine

from tests.support.database import ADMIN_URL, ROOT

pytestmark = pytest.mark.db

spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "db" / "bootstrap.py")
assert spec and spec.loader
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.fixture
def fresh_db() -> Iterator[tuple[str, Engine]]:
    if not ADMIN_URL:
        pytest.skip("TEST_DATABASE_ADMIN_URL not set")
    admin_url = make_url(ADMIN_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    name = f"wassup_b_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = admin_url.set(database=name)
    engine = create_engine(url)
    try:
        yield url.set(drivername="postgresql").render_as_string(hide_password=False), engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_bootstrap_sets_every_login_password_idempotently(
    fresh_db: tuple[str, Engine], capsys: pytest.CaptureFixture[str]
) -> None:
    url, engine = fresh_db
    passwords = {role: secrets.token_urlsafe(32) for role in bootstrap.LOGIN_ROLES}
    bootstrap.bootstrap(url, passwords)
    bootstrap.bootstrap(url, passwords)  # a second run changes nothing and doesn't fail
    out = capsys.readouterr().out
    assert all(p not in out for p in passwords.values())  # never echoed
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rolname, rolcanlogin, rolpassword IS NOT NULL AS has_password "
                "FROM pg_authid WHERE rolname = ANY(:r)"
            ),
            {"r": list(passwords)},
        ).all()
        temp_allowed = conn.execute(
            text("SELECT has_database_privilege('app_core', current_database(), 'TEMPORARY')")
        ).scalar()
    assert {(r.rolname, r.rolcanlogin, r.has_password) for r in rows} == {
        (role, True, True) for role in passwords
    }
    assert temp_allowed is False  # grant_database.sql ran against this database


def test_bootstrap_refuses_missing_or_short_passwords(fresh_db: tuple[str, Engine]) -> None:
    url, _ = fresh_db
    passwords = {role: secrets.token_urlsafe(32) for role in bootstrap.LOGIN_ROLES}
    passwords["app_core"] = "short"
    with pytest.raises(SystemExit, match="app_core"):
        bootstrap.bootstrap(url, passwords)
