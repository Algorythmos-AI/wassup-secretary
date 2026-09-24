"""Structural guarantees checked against the live catalog, so a future migration can't quietly
weaken tenancy: a new tenant table without FORCE RLS, a view without security_invoker, a
SECURITY DEFINER function nobody reviewed, or an app role that can bypass RLS all fail CI."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.support.database import Seed

pytestmark = pytest.mark.db

APP_ROLES = (
    "app_voice",
    "app_core",
    "app_ops",
    "wassup_migrator",
    "wassup_owner",
    "wassup_resolver",
    "wassup_auditor",
)
LOGIN_APP_ROLES = ("app_voice", "app_core", "app_ops", "wassup_migrator")
# The only tables the resolver role may read, and only through FOR SELECT policies.
RESOLVER_READABLE = {"clinics", "clinic_voice_agents", "clinic_phone_numbers", "clinic_memberships"}
# Every SECURITY DEFINER function must be listed here after review, with the role that owns it.
DEFINER_ALLOWLIST = {
    "resolve_clinic_for_call": "wassup_resolver",
    "staff_memberships": "wassup_resolver",
    "active_clinic_ids": "wassup_resolver",
    "audit_log_chain": "wassup_auditor",
}
# The only table the auditor role may touch (the per-clinic audit chain heads: hashes, no data).
AUDITOR_TABLES = {"audit_chain_heads"}
# Tables with clinic_id that are deliberately NOT under RLS (access limited by grants instead).
NON_RLS_WITH_CLINIC = {"retell_events_raw", "audit_chain_heads"}


def test_every_table_with_clinic_id_forces_rls(db_engine: Engine, seed: Seed) -> None:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
                JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'clinic_id' AND NOT a.attisdropped
                WHERE c.relkind IN ('r', 'p')
                """
            )
        ).all()
    assert rows, "expected tenant tables"
    offenders = [
        r.relname
        for r in rows
        if r.relname not in NON_RLS_WITH_CLINIC
        and not (r.relrowsecurity and r.relforcerowsecurity and r.policies > 0)
    ]
    assert offenders == [], f"tables with clinic_id but no forced RLS policy: {offenders}"


def test_no_app_role_is_superuser_or_bypasses_rls(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(:r) AND (rolsuper OR rolbypassrls)"
            ),
            {"r": list(APP_ROLES)},
        ).all()
    assert rows == []


def test_security_definer_functions_are_allowlisted_and_pin_search_path(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT p.proname, p.proconfig
                FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace AND n.nspname = 'public'
                WHERE p.prosecdef
                """
            )
        ).all()
    names = {r.proname for r in rows}
    assert names <= set(DEFINER_ALLOWLIST), (
        f"unreviewed SECURITY DEFINER functions: {names - set(DEFINER_ALLOWLIST)}"
    )

    def pinned_with_temp_last(config: list[str] | None) -> bool:
        # pg_temp is searched FIRST for tables unless named, so it must be listed, and last.
        for setting in config or []:
            if setting.startswith("search_path="):
                path = [p.strip() for p in setting.split("=", 1)[1].split(",")]
                return path[0] == "pg_catalog" and path[-1] == "pg_temp"
        return False

    unsafe = [r.proname for r in rows if not pinned_with_temp_last(r.proconfig)]
    assert unsafe == [], f"definer functions without search_path 'pg_catalog, …, pg_temp': {unsafe}"


def test_login_roles_cannot_create_temporary_objects(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        allowed = [
            role
            for role in LOGIN_APP_ROLES
            if conn.execute(
                text("SELECT has_database_privilege(:r, current_database(), 'TEMPORARY')"),
                {"r": role},
            ).scalar()
        ]
    assert allowed == []


def test_views_use_security_invoker(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.relname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
                WHERE c.relkind = 'v'
                  AND NOT coalesce('security_invoker=true' = ANY(c.reloptions), false)
                  AND NOT coalesce('security_invoker=on' = ANY(c.reloptions), false)
                """
            )
        ).all()
    assert rows == [], f"views bypassing RLS (need security_invoker): {[r.relname for r in rows]}"


def test_no_materialized_views_over_tenant_data(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM pg_matviews WHERE schemaname = 'public'")
        ).scalar()
    assert count == 0


def test_objects_are_owned_by_the_owner_role(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.relname, pg_get_userbyid(c.relowner) AS owner FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
                WHERE c.relkind IN ('r', 'p', 'v', 'S') AND c.relname <> 'alembic_version'
                """
            )
        ).all()
    wrong = [f"{r.relname}:{r.owner}" for r in rows if r.owner != "wassup_owner"]
    assert wrong == []


def test_non_isolation_policies_are_resolver_read_only(db_engine: Engine) -> None:
    """Every policy is either the clinic-isolation policy, or a FOR SELECT policy for the
    resolver role on a routing table. Nothing else may open a table up."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT tablename, policyname, cmd, roles FROM pg_policies WHERE schemaname = 'public'
                """
            )
        ).all()
    unexpected = []
    for r in rows:
        if r.policyname == f"{r.tablename}_clinic_isolation":
            continue
        if (
            r.cmd == "SELECT"
            and list(r.roles) == ["wassup_resolver"]
            and r.tablename in RESOLVER_READABLE
        ):
            continue
        unexpected.append(f"{r.tablename}.{r.policyname}")
    assert unexpected == []


def test_resolver_role_cannot_read_call_data(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT table_name FROM information_schema.role_table_grants
                WHERE grantee = 'wassup_resolver' AND table_schema = 'public'
                """
            )
        ).all()
    assert {r.table_name for r in rows} <= RESOLVER_READABLE | {"staff_users"}


def test_auditor_role_touches_only_the_chain_heads(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT table_name FROM information_schema.role_table_grants
                WHERE grantee = 'wassup_auditor' AND table_schema = 'public'
                """
            )
        ).all()
        app_grants = conn.execute(
            text(
                """
                SELECT grantee FROM information_schema.role_table_grants
                WHERE table_name = 'audit_chain_heads' AND grantee <> ALL(:allowed)
                """
            ),
            {"allowed": ["wassup_auditor", "wassup_owner"]},
        ).all()
    assert {r.table_name for r in rows} == AUDITOR_TABLES
    assert app_grants == []


def test_definer_functions_are_owned_by_their_reviewed_role(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT p.proname, pg_get_userbyid(p.proowner) AS owner FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace AND n.nspname = 'public'
                WHERE p.prosecdef
                """
            )
        ).all()
    assert {r.proname: r.owner for r in rows} == DEFINER_ALLOWLIST
