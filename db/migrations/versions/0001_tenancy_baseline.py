"""Tenancy baseline: clinics, calls and the voice ingestion log, isolated by row-level security.

Every tenant table carries ``clinic_id`` and has ENABLE + FORCE row-level security with the
same policy: a row is visible or writable only when its ``clinic_id`` is in the clinic list the
current transaction declared via ``set_config('app.clinic_ids', '{…}', true)``. No setting means
zero rows. There is no bypass clause: platform-wide jobs declare the full clinic list, resolved
by a narrow SECURITY DEFINER function, so every access is still scoped and auditable.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "clinics",
    "clinic_voice_agents",
    "clinic_phone_numbers",
    "clinic_memberships",
    "calls",
    "call_interactions",
    "messages",
    "promises",
    "patients",
    "tool_invocations",
    "outbox_events",
    "usage_daily",
    "audit_log",
)

SCHEMA = r"""
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;

-- Clinic ids the current transaction may touch. Empty (not NULL) when unset, so every
-- policy comparison is simply false and the query sees zero rows.
CREATE FUNCTION wassup_current_clinics() RETURNS uuid[]
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT COALESCE(NULLIF(current_setting('app.clinic_ids', true), '')::uuid[], '{}'::uuid[]) $$;

CREATE TABLE organizations (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE clinics (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id       uuid GENERATED ALWAYS AS (id) STORED,   -- uniform RLS column
  organization_id uuid NOT NULL REFERENCES organizations(id),
  slug            text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  name            text NOT NULL,
  timezone        text NOT NULL DEFAULT 'Australia/Sydney',
  state           text NOT NULL CHECK (state IN ('NSW','VIC','QLD','WA','SA','TAS','ACT','NT')),
  business_hours  jsonb NOT NULL DEFAULT '{}'::jsonb,
  branding        jsonb NOT NULL DEFAULT '{}'::jsonb,
  features        jsonb NOT NULL DEFAULT '{}'::jsonb,
  alert_contacts  jsonb NOT NULL DEFAULT '[]'::jsonb,
  status          text NOT NULL DEFAULT 'onboarding' CHECK (status IN ('onboarding','active','suspended')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE clinic_voice_agents (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id      uuid NOT NULL REFERENCES clinics(id),
  provider       text NOT NULL DEFAULT 'retell',
  agent_id       text NOT NULL,
  agent_version  integer,
  environment    text NOT NULL CHECK (environment IN ('staging','production')),
  active         boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, agent_id)
);

CREATE TABLE clinic_phone_numbers (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id     uuid NOT NULL REFERENCES clinics(id),
  e164          text NOT NULL UNIQUE CHECK (e164 ~ '^\+[1-9][0-9]{6,14}$'),
  provider      text NOT NULL DEFAULT 'twilio',
  provider_sid  text,
  active        boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE staff_users (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firebase_uid    text NOT NULL UNIQUE,
  email           citext NOT NULL,
  display_name    text,
  platform_admin  boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE clinic_memberships (
  clinic_id      uuid NOT NULL REFERENCES clinics(id),
  staff_user_id  uuid NOT NULL REFERENCES staff_users(id),
  role           text NOT NULL CHECK (role IN ('owner','admin','receptionist','viewer')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (clinic_id, staff_user_id)
);

CREATE TABLE calls (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id            uuid NOT NULL REFERENCES clinics(id),
  provider_call_id     text NOT NULL,
  direction            text NOT NULL CHECK (direction IN ('inbound','outbound')),
  from_number          text,
  to_number            text,
  started_at           timestamptz,
  ended_at             timestamptz,
  duration_seconds     integer CHECK (duration_seconds >= 0),
  cost_usd             numeric(10,4) CHECK (cost_usd >= 0),
  disconnection_reason text,
  summary              text,
  transcript           text,
  sentiment            text,
  intent               text,
  is_priority          boolean NOT NULL DEFAULT false,
  is_reception_action  boolean NOT NULL DEFAULT false,
  local_date           date,
  local_hour           smallint CHECK (local_hour BETWEEN 0 AND 23),
  local_dow            smallint CHECK (local_dow BETWEEN 0 AND 6),
  workflow_status      text NOT NULL DEFAULT 'pending'
                       CHECK (workflow_status IN ('pending','following_up','addressed','no_action_needed')),
  version              integer NOT NULL DEFAULT 1,
  source               text NOT NULL DEFAULT 'webhook' CHECK (source IN ('webhook','reconciler','import')),
  analyzed_at          timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (clinic_id, provider_call_id)
);
CREATE INDEX calls_clinic_started_idx ON calls (clinic_id, started_at DESC, id);
CREATE INDEX calls_clinic_local_date_idx ON calls (clinic_id, local_date);
CREATE INDEX calls_clinic_workflow_idx ON calls (clinic_id, workflow_status);

CREATE TABLE call_interactions (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id            uuid NOT NULL REFERENCES clinics(id),
  call_id              uuid NOT NULL REFERENCES calls(id),
  action_type          text NOT NULL,
  status_from          text,
  status_to            text,
  note                 text CHECK (char_length(note) <= 2000),
  actor_staff_user_id  uuid REFERENCES staff_users(id),
  idempotency_key      text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (clinic_id, idempotency_key)
);
CREATE INDEX call_interactions_call_idx ON call_interactions (clinic_id, call_id, created_at);

CREATE TABLE messages (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id        uuid NOT NULL REFERENCES clinics(id),
  provider_call_id text NOT NULL,
  category         text NOT NULL,
  detail           text NOT NULL CHECK (char_length(detail) <= 4000),
  callback_number  text,
  patient_id       uuid,
  metadata         jsonb,
  dedupe_key       text NOT NULL UNIQUE,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_clinic_call_idx ON messages (clinic_id, provider_call_id);

CREATE TABLE promises (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id        uuid NOT NULL REFERENCES clinics(id),
  provider_call_id text NOT NULL,
  promise_type     text NOT NULL,
  due_at           timestamptz NOT NULL,
  status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open','fulfilled','cancelled')),
  patient_id       uuid,
  dedupe_key       text NOT NULL UNIQUE,
  fulfilled_at     timestamptz,
  fulfilled_by     uuid REFERENCES staff_users(id),
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX promises_clinic_status_due_idx ON promises (clinic_id, status, due_at);

CREATE TABLE patients (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id       uuid NOT NULL REFERENCES clinics(id),
  source_pms_id   text NOT NULL,
  first_name      text,
  last_name       text,
  date_of_birth   date,
  phone           text,
  is_deceased     boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (clinic_id, source_pms_id)
);
-- Lookup filters on the exact DOB first, then fuzzy-matches names within that small set.
CREATE INDEX patients_clinic_dob_idx ON patients (clinic_id, date_of_birth);

-- Raw provider events, stored BEFORE processing so nothing is lost if processing fails.
-- Not tenant-scoped at write time (the clinic is resolved afterwards): access is limited by
-- grants to the voice and ops roles only.
CREATE TABLE retell_events_raw (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  received_at       timestamptz NOT NULL DEFAULT now(),
  event             text NOT NULL,
  provider_call_id  text NOT NULL,
  agent_id          text,
  clinic_id         uuid REFERENCES clinics(id),
  payload           jsonb NOT NULL,
  processed_at      timestamptz,
  error             text,
  UNIQUE (provider_call_id, event)
);

CREATE TABLE quarantine_events (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  received_at  timestamptz NOT NULL DEFAULT now(),
  reason       text NOT NULL,
  agent_id     text,
  payload      jsonb NOT NULL
);

CREATE TABLE tool_invocations (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  clinic_id        uuid NOT NULL REFERENCES clinics(id),
  provider_call_id text NOT NULL,
  tool             text NOT NULL,
  dedupe_key       text NOT NULL UNIQUE,
  args_hash        text NOT NULL,
  result           jsonb,
  latency_ms       integer,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE outbox_events (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  clinic_id     uuid NOT NULL REFERENCES clinics(id),
  event_type    text NOT NULL,
  dedupe_key    text NOT NULL UNIQUE,
  payload       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- ids only, never personal data
  status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','dead')),
  attempts      integer NOT NULL DEFAULT 0,
  last_error    text,
  available_at  timestamptz NOT NULL DEFAULT now(),
  created_at    timestamptz NOT NULL DEFAULT now(),
  processed_at  timestamptz
);
CREATE INDEX outbox_pending_idx ON outbox_events (status, available_at) WHERE status = 'pending';

CREATE TABLE notifications_sent (
  event_id  bigint NOT NULL REFERENCES outbox_events(id),
  channel   text NOT NULL,
  status    text NOT NULL,
  sent_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (event_id, channel)
);

CREATE TABLE usage_daily (
  clinic_id          uuid NOT NULL REFERENCES clinics(id),
  day                date NOT NULL,
  calls              integer NOT NULL DEFAULT 0,
  minutes            numeric(10,2) NOT NULL DEFAULT 0,
  provider_cost_usd  numeric(10,4) NOT NULL DEFAULT 0,
  PRIMARY KEY (clinic_id, day)
);

-- Append-only: app roles get INSERT (and core-api SELECT) only; each row chains the previous
-- row's hash so tampering is detectable.
CREATE TABLE audit_log (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  clinic_id            uuid NOT NULL REFERENCES clinics(id),
  actor_staff_user_id  uuid REFERENCES staff_users(id),
  actor_service        text,
  action               text NOT NULL,
  target_type          text NOT NULL,
  target_id            text,
  at                   timestamptz NOT NULL DEFAULT now(),
  prev_hash            text,
  hash                 text
);
CREATE INDEX audit_log_clinic_at_idx ON audit_log (clinic_id, at DESC);
"""

# SECURITY DEFINER resolvers: the only way to cross the tenancy boundary, each granted to
# exactly one role, each with a pinned search_path. tests/tenancy keeps an allowlist of them.
RESOLVERS = r"""
-- voice-gateway: which clinic does this signed call belong to? The agent AND the dialled
-- number must agree; any mismatch returns NULL (the caller quarantines the event).
CREATE FUNCTION resolve_clinic_for_call(p_agent_id text, p_to_number text) RETURNS uuid
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
  AS $$
    SELECT a.clinic_id
    FROM clinic_voice_agents a
    JOIN clinics c ON c.id = a.clinic_id AND c.status <> 'suspended'
    WHERE a.agent_id = p_agent_id AND a.active
      AND (p_to_number IS NULL OR EXISTS (
            SELECT 1 FROM clinic_phone_numbers n
            WHERE n.clinic_id = a.clinic_id AND n.e164 = p_to_number AND n.active))
  $$;

-- core-api: which clinics may this signed-in staff member see?
CREATE FUNCTION staff_clinic_ids(p_firebase_uid text) RETURNS uuid[]
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
  AS $$
    SELECT COALESCE(array_agg(m.clinic_id), '{}'::uuid[])
    FROM staff_users u JOIN clinic_memberships m ON m.staff_user_id = u.id
    WHERE u.firebase_uid = p_firebase_uid
  $$;

-- ops-worker: platform jobs run over every non-suspended clinic, still through RLS.
CREATE FUNCTION active_clinic_ids() RETURNS uuid[]
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
  AS $$ SELECT COALESCE(array_agg(id), '{}'::uuid[]) FROM clinics WHERE status <> 'suspended' $$;

-- The resolvers run as wassup_resolver: it can READ only the routing/membership tables, through
-- explicit FOR SELECT policies. It never sees calls, messages, patients or any other data.
GRANT SELECT ON clinics, clinic_voice_agents, clinic_phone_numbers, clinic_memberships, staff_users
  TO wassup_resolver;
CREATE POLICY clinics_resolver_read ON clinics FOR SELECT TO wassup_resolver USING (true);
CREATE POLICY clinic_voice_agents_resolver_read ON clinic_voice_agents FOR SELECT TO wassup_resolver USING (true);
CREATE POLICY clinic_phone_numbers_resolver_read ON clinic_phone_numbers FOR SELECT TO wassup_resolver USING (true);
CREATE POLICY clinic_memberships_resolver_read ON clinic_memberships FOR SELECT TO wassup_resolver USING (true);
ALTER FUNCTION resolve_clinic_for_call(text, text) OWNER TO wassup_resolver;
ALTER FUNCTION staff_clinic_ids(text) OWNER TO wassup_resolver;
ALTER FUNCTION active_clinic_ids() OWNER TO wassup_resolver;

REVOKE ALL ON FUNCTION resolve_clinic_for_call(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION staff_clinic_ids(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION active_clinic_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_clinic_for_call(text, text) TO app_voice;
GRANT EXECUTE ON FUNCTION staff_clinic_ids(text) TO app_core;
GRANT EXECUTE ON FUNCTION active_clinic_ids() TO app_ops;
"""

GRANTS = r"""
-- New tables grant nothing to PUBLIC; each role gets exactly what it needs.
-- voice-gateway: writes what a call produces; reads only what a call needs.
GRANT SELECT ON clinics TO app_voice;
GRANT SELECT, INSERT, UPDATE ON calls, messages, promises, tool_invocations TO app_voice;
GRANT SELECT, INSERT, UPDATE ON retell_events_raw TO app_voice;
GRANT INSERT ON quarantine_events, outbox_events, audit_log TO app_voice;
-- INSERT … ON CONFLICT (dedupe_key) DO NOTHING needs SELECT on the conflict column only.
GRANT SELECT (dedupe_key) ON outbox_events TO app_voice;
GRANT SELECT (id, clinic_id, first_name, last_name, date_of_birth, phone, is_deceased)
  ON patients TO app_voice;

-- core-api: staff dashboard reads, workflow updates and audit.
GRANT SELECT ON clinics, clinic_voice_agents, clinic_phone_numbers, clinic_memberships TO app_core;
GRANT SELECT ON staff_users TO app_core;
GRANT SELECT ON calls, messages, promises, usage_daily TO app_core;
GRANT UPDATE (workflow_status, version, updated_at) ON calls TO app_core;
GRANT UPDATE (status, fulfilled_at, fulfilled_by) ON promises TO app_core;
GRANT SELECT, INSERT ON call_interactions TO app_core;
GRANT SELECT, INSERT ON audit_log TO app_core;

-- ops-worker: outbox processing, reconciliation, usage and alerts.
GRANT SELECT ON clinics, clinic_voice_agents, clinic_phone_numbers TO app_ops;
GRANT SELECT, INSERT, UPDATE ON calls TO app_ops;
GRANT SELECT, UPDATE ON outbox_events TO app_ops;
GRANT SELECT, INSERT ON notifications_sent TO app_ops;
GRANT SELECT, INSERT, UPDATE ON usage_daily TO app_ops;
GRANT SELECT, UPDATE ON retell_events_raw TO app_ops;
GRANT SELECT ON quarantine_events TO app_ops;
GRANT INSERT ON audit_log TO app_ops;
"""


def _rls_sql() -> str:
    parts = []
    for table in TENANT_TABLES:
        parts.append(
            f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY {table}_clinic_isolation ON {table}
  USING (clinic_id = ANY ((SELECT wassup_current_clinics())::uuid[]))
  WITH CHECK (clinic_id = ANY ((SELECT wassup_current_clinics())::uuid[]));
"""
        )
    return "\n".join(parts)


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(SCHEMA)
    op.execute(_rls_sql())
    op.execute(RESOLVERS)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    # Forward-only in production (roll back by redeploying the previous image). This exists for
    # local development only.
    op.execute("SET ROLE wassup_owner")
    op.execute(
        "DROP FUNCTION IF EXISTS resolve_clinic_for_call(text, text), staff_clinic_ids(text), "
        "active_clinic_ids() CASCADE"
    )
    for table in (
        "audit_log",
        "usage_daily",
        "notifications_sent",
        "outbox_events",
        "tool_invocations",
        "quarantine_events",
        "retell_events_raw",
        "patients",
        "promises",
        "messages",
        "call_interactions",
        "calls",
        "clinic_memberships",
        "staff_users",
        "clinic_phone_numbers",
        "clinic_voice_agents",
        "clinics",
        "organizations",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS wassup_current_clinics()")
    op.execute("RESET ROLE")
