"""Team management: invitations, self-service enrolment, and membership changes by admins.

Until now a clinic's staff were added by hand in SQL. An admin can now invite a colleague by
email; on that person's next sign-in core-api creates their membership itself, under the invited
clinic's row-level security, so no privileged writer is needed:

- ``clinic_invitations`` is a routing table like ``clinic_memberships``: clinic-isolated, and
  readable by the resolver role so ``invited_clinics(email)`` can find a newcomer's invitations
  before they have any membership at all.
- ``enrol_staff(uid, email)`` (resolver, definer) creates or refreshes the ``staff_users`` row for
  a verified sign-in. It is the only write the resolver role can make, and only to that table.
- ``clinic_members(clinic_id)`` (resolver, definer) lists a clinic's members with their emails,
  but only for a clinic in the caller's current scope, so it can't be used to enumerate anyone.
- ``app_core`` may insert, change the role of, and delete memberships, and manage invitations,
  always under the clinic-isolation policy. Every change is audited by the API.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

SQL = r"""
CREATE TABLE clinic_invitations (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id    uuid NOT NULL REFERENCES clinics(id),
  email        citext NOT NULL CHECK (char_length(email) <= 254 AND position('@' in email) > 1),
  role         text NOT NULL CHECK (role IN ('owner','admin','receptionist','viewer')),
  invited_by   uuid NOT NULL REFERENCES staff_users(id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  accepted_at  timestamptz,
  accepted_by  uuid REFERENCES staff_users(id),
  revoked_at   timestamptz
);
-- One open invitation per person per clinic.
CREATE UNIQUE INDEX clinic_invitations_open ON clinic_invitations (clinic_id, email)
  WHERE accepted_at IS NULL AND revoked_at IS NULL;
CREATE INDEX clinic_invitations_email_open ON clinic_invitations (email)
  WHERE accepted_at IS NULL AND revoked_at IS NULL;
ALTER TABLE clinic_invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_invitations FORCE ROW LEVEL SECURITY;
CREATE POLICY clinic_invitations_clinic_isolation ON clinic_invitations
  USING (clinic_id = ANY ((SELECT wassup_current_clinics())::uuid[]))
  WITH CHECK (clinic_id = ANY ((SELECT wassup_current_clinics())::uuid[]));
CREATE POLICY clinic_invitations_resolver_read ON clinic_invitations FOR SELECT TO wassup_resolver USING (true);
GRANT SELECT ON clinic_invitations TO wassup_resolver;
GRANT SELECT, INSERT ON clinic_invitations TO app_core;
GRANT UPDATE (accepted_at, accepted_by, revoked_at) ON clinic_invitations TO app_core;

-- Memberships: core-api may now write them, under the isolation policy.
GRANT INSERT, DELETE ON clinic_memberships TO app_core;
GRANT UPDATE (role) ON clinic_memberships TO app_core;

-- The resolver may create the staff row for a verified sign-in: nothing else on that table.
GRANT INSERT ON staff_users TO wassup_resolver;
GRANT UPDATE (email, display_name) ON staff_users TO wassup_resolver;

CREATE FUNCTION enrol_staff(p_firebase_uid text, p_email text, p_display_name text)
  RETURNS uuid
  LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
  AS $$
    INSERT INTO staff_users (firebase_uid, email, display_name)
    VALUES (p_firebase_uid, p_email, p_display_name)
    ON CONFLICT (firebase_uid) DO UPDATE
      SET email = EXCLUDED.email,
          display_name = COALESCE(EXCLUDED.display_name, staff_users.display_name)
    RETURNING id
  $$;
ALTER FUNCTION enrol_staff(text, text, text) OWNER TO wassup_resolver;
REVOKE ALL ON FUNCTION enrol_staff(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION enrol_staff(text, text, text) TO app_core;

CREATE FUNCTION invited_clinics(p_email text)
  RETURNS TABLE (invitation_id uuid, clinic_id uuid, role text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
  AS $$
    SELECT i.id, i.clinic_id, i.role
    FROM clinic_invitations i JOIN clinics c ON c.id = i.clinic_id AND c.status <> 'suspended'
    WHERE i.email = p_email::citext AND i.accepted_at IS NULL AND i.revoked_at IS NULL
  $$;
ALTER FUNCTION invited_clinics(text) OWNER TO wassup_resolver;
REVOKE ALL ON FUNCTION invited_clinics(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION invited_clinics(text) TO app_core;

-- Members of a clinic that is in the caller's scope; any other clinic yields no rows.
CREATE FUNCTION clinic_members(p_clinic_id uuid)
  RETURNS TABLE (staff_user_id uuid, email text, display_name text, role text, since timestamptz)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
  AS $$
    SELECT u.id, u.email::text, u.display_name, m.role, m.created_at
    FROM clinic_memberships m JOIN staff_users u ON u.id = m.staff_user_id
    WHERE m.clinic_id = p_clinic_id
      AND p_clinic_id = ANY ((SELECT wassup_current_clinics())::uuid[])
    ORDER BY m.created_at, u.email
  $$;
ALTER FUNCTION clinic_members(uuid) OWNER TO wassup_resolver;
REVOKE ALL ON FUNCTION clinic_members(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_members(uuid) TO app_core;
"""


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(SQL)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
