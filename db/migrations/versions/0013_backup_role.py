"""The backup role can read every table and sequence, now and in future.

``wassup_backup`` (created by ``db/roles.sql``; re-run the db-admin bootstrap before this
migration on an existing database) is the one role that bypasses row-level security, because a
backup must hold every clinic's rows. It gets SELECT on everything and nothing else; default
privileges make tables and sequences added by later migrations readable too. It may not execute
any SECURITY DEFINER resolver (those are granted to specific app roles, never PUBLIC).

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

CHECK = r"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wassup_backup') THEN
    RAISE EXCEPTION 'role wassup_backup is missing: run the db-admin bootstrap (db/roles.sql) first';
  END IF;
END
$$;
"""

SQL = r"""
-- Only what the owner role owns (ON ALL TABLES would also try alembic_version, the migrator's).
DO $$
DECLARE rel record;
BEGIN
  FOR rel IN
    SELECT c.relname, c.relkind
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
    WHERE c.relkind IN ('r', 'p', 'S') AND pg_get_userbyid(c.relowner) = 'wassup_owner'
  LOOP
    EXECUTE format('GRANT SELECT ON %s public.%I TO wassup_backup',
                   CASE WHEN rel.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END, rel.relname);
  END LOOP;
END
$$;
ALTER DEFAULT PRIVILEGES FOR ROLE wassup_owner IN SCHEMA public
  GRANT SELECT ON TABLES TO wassup_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE wassup_owner IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO wassup_backup;
"""


def upgrade() -> None:
    op.execute(CHECK)
    op.execute("SET ROLE wassup_owner")
    op.execute(SQL)
    op.execute("RESET ROLE")
    # alembic_version belongs to the migrator (Alembic creates it outside any migration); the
    # backup records the schema revision it was taken at.
    op.execute("GRANT SELECT ON alembic_version TO wassup_backup")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
