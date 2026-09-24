"""Regression tests for the security review: temporary-object shadowing of definer functions, and
the tamper-evident audit chain."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import ProgrammingError

from tests.support.database import Seed, as_role

pytestmark = pytest.mark.db


def _staff_member_of(conn: Connection, clinic: uuid.UUID) -> tuple[str, uuid.UUID]:
    uid = f"uid-{uuid.uuid4().hex[:10]}"
    staff_id = conn.execute(
        text("INSERT INTO staff_users (firebase_uid, email) VALUES (:u, :e) RETURNING id"),
        {"u": uid, "e": f"{uid}@example.test"},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO clinic_memberships (clinic_id, staff_user_id, role) "
            "VALUES (:c, :s, 'receptionist')"
        ),
        {"c": clinic, "s": staff_id},
    )
    return uid, staff_id


def test_app_role_cannot_create_temporary_objects(db_engine: Engine) -> None:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "app_core", None)
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("CREATE TEMP VIEW clinic_memberships AS SELECT 1 AS x"))


def test_temp_view_cannot_forge_memberships(db_engine: Engine, seed: Seed) -> None:
    """The reviewer's attack, with the TEMPORARY privilege taken out of the picture (the admin
    session can still create temp objects): a forged ``clinic_memberships`` in pg_temp must not be
    read by ``staff_memberships()``, because its search_path pins pg_temp last."""
    with db_engine.connect() as conn, conn.begin():
        uid, staff_id = _staff_member_of(conn, seed.clinic_a)
        conn.execute(
            text(
                "CREATE TEMP VIEW clinic_memberships AS "
                f"SELECT '{staff_id}'::uuid AS staff_user_id, '{seed.clinic_b}'::uuid AS clinic_id, "
                "'admin'::text AS role"
            )
        )
        conn.execute(text("GRANT SELECT ON clinic_memberships TO wassup_resolver"))
        as_role(conn, "app_core", None)
        clinics = {
            r.clinic_id
            for r in conn.execute(text("SELECT * FROM staff_memberships(:u)"), {"u": uid})
        }
    assert clinics == {seed.clinic_a}


# ---------- audit chain ----------

_VERIFY = text(
    """
    SELECT chain_seq, prev_hash, hash,
           lag(hash) OVER (ORDER BY chain_seq) AS expected_prev,
           audit_row_hash(chain_seq, prev_hash, clinic_id, actor_staff_user_id, actor_service,
                          action, target_type, target_id, detail, at) AS recomputed
    FROM audit_log WHERE clinic_id = :c ORDER BY chain_seq
    """
)


def _chain_problems(db_engine: Engine, clinic: uuid.UUID) -> list[str]:
    with db_engine.connect() as conn:
        rows = conn.execute(_VERIFY, {"c": clinic}).all()
    problems = []
    for position, row in enumerate(rows, start=1):
        if row.chain_seq != position:
            problems.append(f"gap_or_fork_at_{position}")
        if row.prev_hash != row.expected_prev:
            problems.append(f"broken_link_at_{row.chain_seq}")
        if row.hash != row.recomputed:
            problems.append(f"altered_row_{row.chain_seq}")
    return problems


def _write_audit(db_engine: Engine, clinic: uuid.UUID, role: str, action: str) -> None:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, role, [clinic])
        conn.execute(
            text(
                "INSERT INTO audit_log (clinic_id, actor_service, action, target_type, "
                "chain_seq, prev_hash, hash) VALUES (:c, 'test', :a, 'call', 999, 'forged', 'forged')"
            ),
            {"c": clinic, "a": action},
        )


def _new_clinic(db_engine: Engine) -> uuid.UUID:
    clinic = uuid.uuid4()
    with db_engine.connect() as conn, conn.begin():
        org = conn.execute(
            text("INSERT INTO organizations (name) VALUES ('Audit Org') RETURNING id")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO clinics (id, organization_id, slug, name, state) "
                "VALUES (:id, :o, :s, :s, 'NSW')"
            ),
            {"id": clinic, "o": org, "s": f"audit-{clinic.hex[:8]}"},
        )
    return clinic


def test_audit_rows_form_an_unbroken_chain_under_concurrency(db_engine: Engine) -> None:
    clinic = _new_clinic(db_engine)
    roles = ["app_core", "app_voice", "app_ops", "app_core"]

    def writer(role: str) -> None:
        for i in range(10):
            _write_audit(db_engine, clinic, role, f"test.{role}.{i}")

    threads = [threading.Thread(target=writer, args=(r,)) for r in roles]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with db_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE clinic_id = :c"), {"c": clinic}
        ).scalar()
    assert count == 40
    assert _chain_problems(db_engine, clinic) == []  # the forged chain values were overwritten


def test_tampering_breaks_verification(db_engine: Engine) -> None:
    clinic = _new_clinic(db_engine)
    for i in range(3):
        _write_audit(db_engine, clinic, "app_core", f"test.tamper.{i}")
    assert _chain_problems(db_engine, clinic) == []
    with db_engine.connect() as conn, conn.begin():  # a superuser rewriting history
        conn.execute(
            text("UPDATE audit_log SET action = 'innocent' WHERE clinic_id = :c AND chain_seq = 2"),
            {"c": clinic},
        )
    assert _chain_problems(db_engine, clinic) == ["altered_row_2"]
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text("DELETE FROM audit_log WHERE clinic_id = :c AND chain_seq = 2"), {"c": clinic}
        )
    problems = _chain_problems(db_engine, clinic)
    assert "gap_or_fork_at_2" in problems and "broken_link_at_3" in problems


@pytest.mark.parametrize("role", ["app_core", "app_voice", "app_ops"])
def test_app_roles_cannot_rewrite_audit_rows_or_chain_heads(
    db_engine: Engine, seed: Seed, role: str
) -> None:
    for statement in (
        "UPDATE audit_log SET action = 'x'",
        "DELETE FROM audit_log",
        "UPDATE audit_chain_heads SET hash = 'x'",
        "SELECT * FROM audit_chain_heads",
    ):
        with db_engine.connect() as conn, conn.begin():
            as_role(conn, role, [seed.clinic_a])
            with pytest.raises(ProgrammingError, match="permission denied"):
                conn.execute(text(statement))
