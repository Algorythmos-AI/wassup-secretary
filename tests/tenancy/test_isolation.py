"""Clinic isolation, enforced by the database itself (row-level security)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tests.support.database import Seed, as_role

pytestmark = pytest.mark.db


def _call_ids(db_engine: Engine, role: str, clinics: list[uuid.UUID] | None) -> set[uuid.UUID]:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, role, clinics)
        return {row[0] for row in conn.execute(text("SELECT id FROM calls"))}


@pytest.mark.parametrize("role", ["app_core", "app_voice", "app_ops"])
def test_each_role_sees_only_its_declared_clinic(db_engine: Engine, seed: Seed, role: str) -> None:
    assert _call_ids(db_engine, role, [seed.clinic_a]) == {seed.call_a}
    assert _call_ids(db_engine, role, [seed.clinic_b]) == {seed.call_b}


@pytest.mark.parametrize("role", ["app_core", "app_voice", "app_ops", "wassup_owner"])
def test_no_context_means_zero_rows(db_engine: Engine, seed: Seed, role: str) -> None:
    assert _call_ids(db_engine, role, None) == set()


def test_multi_clinic_context_sees_both(db_engine: Engine, seed: Seed) -> None:
    assert _call_ids(db_engine, "app_core", [seed.clinic_a, seed.clinic_b]) == {
        seed.call_a,
        seed.call_b,
    }


def test_cannot_write_into_another_clinic(db_engine: Engine, seed: Seed) -> None:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "app_voice", [seed.clinic_a])
        with pytest.raises(ProgrammingError, match="row-level security"):
            conn.execute(
                text(
                    "INSERT INTO calls (clinic_id, provider_call_id, direction) "
                    "VALUES (:b, 'call_cross_tenant', 'inbound')"
                ),
                {"b": seed.clinic_b},
            )


def test_cannot_move_a_row_to_another_clinic(db_engine: Engine, seed: Seed) -> None:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "app_ops", [seed.clinic_a])
        with pytest.raises(ProgrammingError, match="row-level security"):
            conn.execute(
                text("UPDATE calls SET clinic_id = :b WHERE id = :id"),
                {"b": seed.clinic_b, "id": seed.call_a},
            )


def test_owner_is_also_subject_to_rls(db_engine: Engine, seed: Seed) -> None:
    """FORCE ROW LEVEL SECURITY: even the table owner needs a clinic context."""
    assert _call_ids(db_engine, "wassup_owner", [seed.clinic_a]) == {seed.call_a}


def test_resolver_requires_agent_and_number_to_agree(db_engine: Engine, seed: Seed) -> None:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "app_voice", None)
        resolve = text("SELECT resolve_clinic_for_call(:agent, :to)")
        assert (
            conn.execute(resolve, {"agent": "agent_test_a", "to": "+61400000101"}).scalar()
            == seed.clinic_a
        )
        # Clinic A's agent answering clinic B's number is a misconfiguration → quarantine.
        assert (
            conn.execute(resolve, {"agent": "agent_test_a", "to": "+61400000102"}).scalar() is None
        )
        assert (
            conn.execute(resolve, {"agent": "agent_unknown", "to": "+61400000101"}).scalar() is None
        )


@pytest.mark.parametrize(
    ("role", "function_call"),
    [
        ("app_core", "SELECT resolve_clinic_for_call('agent_test_a', NULL)"),
        ("app_voice", "SELECT staff_clinic_ids('uid')"),
        ("app_voice", "SELECT active_clinic_ids()"),
        ("app_core", "SELECT active_clinic_ids()"),
    ],
)
def test_resolvers_are_granted_to_exactly_one_role(
    db_engine: Engine, role: str, function_call: str
) -> None:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, role, None)
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text(function_call))


@pytest.mark.parametrize(
    ("role", "statement"),
    [
        ("app_core", "SELECT count(*) FROM retell_events_raw"),
        ("app_core", "SELECT count(*) FROM patients"),
        ("app_core", "SELECT count(*) FROM tool_invocations"),
        ("app_voice", "SELECT count(*) FROM call_interactions"),
        ("app_voice", "SELECT count(*) FROM staff_users"),
        ("app_voice", "SELECT count(*) FROM audit_log"),
        ("app_voice", "SELECT source_pms_id FROM patients"),
        ("app_ops", "SELECT count(*) FROM patients"),
    ],
)
def test_least_privilege_grants(db_engine: Engine, role: str, statement: str) -> None:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, role, None)
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text(statement))


@pytest.mark.parametrize("role", ["app_core", "app_voice", "app_ops"])
def test_audit_log_is_append_only(db_engine: Engine, seed: Seed, role: str) -> None:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, role, [seed.clinic_a])
        for statement in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
            with pytest.raises(DBAPIError, match="permission denied"), conn.begin_nested():
                conn.execute(text(statement))
