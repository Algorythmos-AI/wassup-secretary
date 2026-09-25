"""Harden the SECURITY DEFINER functions, chain the audit log, bind idempotency keys to a call.

1. ``search_path`` on every definer function becomes ``pg_catalog, public, pg_temp``. Postgres
   searches ``pg_temp`` *first* for tables and views unless it is named explicitly, so a session
   able to create temporary objects could shadow ``clinic_memberships`` with a forged view and
   make ``staff_memberships()`` return clinics it does not belong to. ``grant_database.sql`` also
   revokes TEMPORARY from PUBLIC, so the app roles can't create temporary objects at all. Either
   control alone closes the hole.
2. ``staff_clinic_ids()`` was superseded by ``staff_memberships()`` (0003) and is dropped: an
   unused privileged function is attack surface with no benefit.
3. ``audit_log`` rows are hash-chained per clinic. A BEFORE INSERT trigger (SECURITY DEFINER,
   owned by the NOLOGIN ``wassup_auditor`` role) takes the clinic's chain head under a row lock,
   so concurrent writers are serialised per clinic and the chain has no gaps or forks. Each row
   stores ``chain_seq``, ``prev_hash`` and ``hash = sha256(canonical row)``; changing, deleting or
   re-ordering any row breaks verification (``audit_row_hash`` recomputes it). App roles only ever
   INSERT audit rows, and the trigger overwrites whatever chain values they supply.
4. ``call_interactions`` records a hash of the request and the resulting version, so a replayed
   ``Idempotency-Key`` returns the original answer and a key reused for a *different* request is
   rejected instead of silently skipping it.

Needs the roles.sql / grant_database.sql from this release (adds ``wassup_auditor``). Nothing has
been deployed before this revision, so there are no unchained audit rows to back-fill.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SQL = r"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wassup_auditor') THEN
    RAISE EXCEPTION 'role wassup_auditor is missing: run db/roles.sql and db/grant_database.sql from this release first';
  END IF;
END
$$;

-- 1. pg_temp last, explicitly, on every definer function.
ALTER FUNCTION resolve_clinic_for_call(text, text) SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION active_clinic_ids() SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION staff_memberships(text) SET search_path = pg_catalog, public, pg_temp;

-- 2. Unused privileged function.
DROP FUNCTION staff_clinic_ids(text);

-- 3. Audit hash chain.
ALTER TABLE audit_log ADD COLUMN chain_seq bigint;
ALTER TABLE audit_log ADD COLUMN detail jsonb;
CREATE UNIQUE INDEX audit_log_chain_idx ON audit_log (clinic_id, chain_seq);

-- One row per clinic: the latest sequence number and hash. It holds no personal data, and no app
-- role has any privilege on it; only the trigger (running as wassup_auditor) reads or writes it.
CREATE TABLE audit_chain_heads (
  clinic_id  uuid PRIMARY KEY REFERENCES clinics(id),
  seq        bigint NOT NULL,
  hash       text NOT NULL
);
REVOKE ALL ON audit_chain_heads FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON audit_chain_heads TO wassup_auditor;

-- The canonical form of an audit row. jsonb's text rendering is deterministic (fixed key order,
-- standard escaping), and the timestamp is hashed as integer microseconds so the session's
-- TimeZone / DateStyle can't change the result. Bump the leading version tag if this changes.
CREATE FUNCTION audit_row_hash(
  p_chain_seq bigint, p_prev_hash text, p_clinic_id uuid, p_actor_staff_user_id uuid,
  p_actor_service text, p_action text, p_target_type text, p_target_id text,
  p_detail jsonb, p_at timestamptz
) RETURNS text
  LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp
  AS $$
    SELECT encode(sha256(convert_to(jsonb_build_array(
      'audit.v1', p_chain_seq, p_prev_hash, p_clinic_id, p_actor_staff_user_id, p_actor_service,
      p_action, p_target_type, p_target_id, p_detail,
      (extract(epoch FROM p_at) * 1000000)::bigint
    )::text, 'UTF8')), 'hex')
  $$;

CREATE FUNCTION audit_log_chain() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp
  AS $$
  DECLARE
    head_seq  bigint;
    head_hash text;
  BEGIN
    INSERT INTO audit_chain_heads (clinic_id, seq, hash) VALUES (NEW.clinic_id, 0, '')
      ON CONFLICT (clinic_id) DO NOTHING;
    -- The row lock serialises writers for this clinic until their transaction ends.
    SELECT h.seq, h.hash INTO head_seq, head_hash
      FROM audit_chain_heads h WHERE h.clinic_id = NEW.clinic_id FOR UPDATE;
    NEW.chain_seq := head_seq + 1;
    NEW.prev_hash := NULLIF(head_hash, '');
    NEW.hash := audit_row_hash(NEW.chain_seq, NEW.prev_hash, NEW.clinic_id,
      NEW.actor_staff_user_id, NEW.actor_service, NEW.action, NEW.target_type, NEW.target_id,
      NEW.detail, NEW.at);
    UPDATE audit_chain_heads SET seq = NEW.chain_seq, hash = NEW.hash
      WHERE clinic_id = NEW.clinic_id;
    RETURN NEW;
  END
  $$;
ALTER FUNCTION audit_log_chain() OWNER TO wassup_auditor;
REVOKE ALL ON FUNCTION audit_log_chain() FROM PUBLIC;
CREATE TRIGGER audit_log_chain BEFORE INSERT ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_chain();

-- 4. Idempotency keys remember what they were used for.
ALTER TABLE call_interactions ADD COLUMN request_hash text;
ALTER TABLE call_interactions ADD COLUMN result_version integer;
"""


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(SQL)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
