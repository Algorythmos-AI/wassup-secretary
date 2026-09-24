-- Cluster-level roles for WASSUP Secretary. Run ONCE per Postgres cluster by an admin
-- (not by Alembic: the migrator is deliberately not a superuser). Idempotent.
--
--   wassup_owner     NOLOGIN   owns every schema object; migrations SET ROLE to it
--   wassup_migrator  LOGIN     runs Alembic; member of wassup_owner; nothing else
--   wassup_resolver  NOLOGIN   owns the SECURITY DEFINER resolver functions; may only READ the
--                              routing/membership tables (see migration 0001)
--   wassup_auditor   NOLOGIN   owns the audit-log chaining trigger; may only touch the chain-head
--                              table, never the audit rows or any tenant data (migration 0004)
--   app_voice        LOGIN     voice-gateway
--   app_core         LOGIN     core-api
--   app_ops          LOGIN     ops-worker
--
-- None of them is SUPERUSER or BYPASSRLS, so row-level security always applies.
-- Passwords are set out-of-band by the operator (never in git):
--   ALTER ROLE app_core PASSWORD '...';

DO $$
DECLARE r text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wassup_owner') THEN
    CREATE ROLE wassup_owner NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wassup_resolver') THEN
    CREATE ROLE wassup_resolver NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wassup_auditor') THEN
    CREATE ROLE wassup_auditor NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wassup_migrator') THEN
    CREATE ROLE wassup_migrator LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE;
  END IF;
  FOREACH r IN ARRAY ARRAY['app_voice', 'app_core', 'app_ops'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOINHERIT', r);
    END IF;
  END LOOP;
END
$$;

GRANT wassup_owner TO wassup_migrator;
-- The owner must be a member of the resolver role to hand it ownership of the resolver functions.
GRANT wassup_resolver TO wassup_owner;
GRANT wassup_auditor TO wassup_owner;

-- Per-role guard rails: one slow dashboard query can never starve the phone path.
ALTER ROLE app_voice SET statement_timeout = '1s';
ALTER ROLE app_core  SET statement_timeout = '5s';
ALTER ROLE app_ops   SET statement_timeout = '60s';
ALTER ROLE app_voice CONNECTION LIMIT 20;
ALTER ROLE app_core  CONNECTION LIMIT 40;
ALTER ROLE app_ops   CONNECTION LIMIT 10;
