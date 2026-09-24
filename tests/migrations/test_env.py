"""The production migration environment (db/migrations/env.py) against throwaway databases, with
a throwaway set of migrations: a failure part-way must leave the previous revision intact, and
concurrent deploys must apply each migration exactly once."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.engine import Engine

from tests.support.database import ADMIN_URL, ROOT, upgrade

pytestmark = pytest.mark.db

MIGRATION = """
from alembic import op
revision = "{rev}"
down_revision = {down}
branch_labels = None
depends_on = None
def upgrade():
    {body}
def downgrade():
    pass
"""


def _scripts(tmp_path: Path, bodies: list[str]) -> Path:
    shutil.copy(ROOT / "db" / "migrations" / "env.py", tmp_path / "env.py")
    shutil.copy(ROOT / "db" / "migrations" / "script.py.mako", tmp_path / "script.py.mako")
    versions = tmp_path / "versions"
    versions.mkdir()
    for i, body in enumerate(bodies, start=1):
        down = repr(f"m{i - 1}") if i > 1 else "None"
        (versions / f"m{i}.py").write_text(MIGRATION.format(rev=f"m{i}", down=down, body=body))
    return tmp_path


@pytest.fixture
def scratch_db() -> Iterator[Engine]:
    if not ADMIN_URL:
        pytest.skip("TEST_DATABASE_ADMIN_URL not set")
    admin_url = make_url(ADMIN_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    name = f"wassup_m_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(admin_url.set(database=name))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _state(engine: Engine) -> tuple[str | None, set[str]]:
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        tables = set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
    return version, tables - {"alembic_version"}


def test_a_failing_migration_leaves_the_previous_revision_intact(
    scratch_db: Engine, tmp_path: Path
) -> None:
    scripts = _scripts(
        tmp_path,
        [
            'op.execute("CREATE TABLE t_ok (id int)")',
            'op.execute("CREATE TABLE t_partial (id int)"); op.execute("SELECT 1/0")',
        ],
    )
    with scratch_db.connect() as conn, pytest.raises(Exception, match="division by zero"):
        upgrade(conn, "head", scripts)
    assert _state(scratch_db) == ("m1", {"t_ok"})  # no half-applied m2
    # The lock was released: a later deploy can still take it.
    with scratch_db.connect() as conn:
        assert conn.execute(text("SELECT pg_try_advisory_lock(727001)")).scalar() is True


def test_autocommit_connections_are_refused(scratch_db: Engine, tmp_path: Path) -> None:
    scripts = _scripts(tmp_path, ['op.execute("CREATE TABLE t (id int)")'])
    with (
        scratch_db.connect().execution_options(isolation_level="AUTOCOMMIT") as conn,
        pytest.raises(SystemExit, match="transactional"),
    ):
        upgrade(conn, "head", scripts)


def test_concurrent_deploys_apply_each_migration_once(scratch_db: Engine, tmp_path: Path) -> None:
    """Three replicas' pre-deploy steps start together, through the real CLI and URL handling."""
    scripts = _scripts(
        tmp_path,
        [
            'op.execute("SELECT pg_sleep(0.5)"); op.execute("CREATE TABLE t_once (id int)")',
            'op.execute("CREATE TABLE t_second (id int)")',
        ],
    )
    ini = tmp_path / "alembic.ini"
    ini.write_text(f"[alembic]\nscript_location = {scripts}\n")
    # A plain postgresql:// URL, as platforms provide it.
    url = scratch_db.url.set(drivername="postgresql").render_as_string(hide_password=False)
    env = {**os.environ, "WASSUP_MIGRATION_DATABASE_URL": url}
    deploys = [
        subprocess.Popen(  # noqa: S603 — fixed argv, test-only
            [sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        for _ in range(3)
    ]
    outputs = [d.communicate(timeout=60)[0].decode() for d in deploys]
    assert [d.returncode for d in deploys] == [0, 0, 0], outputs
    assert _state(scratch_db) == ("m2", {"t_once", "t_second"})
