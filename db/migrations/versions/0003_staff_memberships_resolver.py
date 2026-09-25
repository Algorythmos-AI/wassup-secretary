"""Close a cross-clinic read path: app_core may no longer read the staff_users table.

staff_users has no clinic_id (a person can belong to several clinics), so row-level security
can't scope it; with a plain SELECT grant, any core-api query could list every clinic's staff.
Staff identity is now resolved only through ``staff_memberships(uid)``, a reviewed SECURITY
DEFINER function (owned by the read-only wassup_resolver role) that returns the caller's own
memberships and nothing else.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(
        r"""
        REVOKE SELECT ON staff_users FROM app_core;

        CREATE FUNCTION staff_memberships(p_firebase_uid text)
          RETURNS TABLE (staff_user_id uuid, clinic_id uuid, role text)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
          AS $$
            SELECT u.id, m.clinic_id, m.role
            FROM staff_users u JOIN clinic_memberships m ON m.staff_user_id = u.id
            JOIN clinics c ON c.id = m.clinic_id AND c.status <> 'suspended'
            WHERE u.firebase_uid = p_firebase_uid
          $$;
        ALTER FUNCTION staff_memberships(text) OWNER TO wassup_resolver;
        REVOKE ALL ON FUNCTION staff_memberships(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION staff_memberships(text) TO app_core;
        """
    )
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
