-- Run once per database (as the database owner / admin) after roles.sql.
-- :"dbname" is supplied by psql -v dbname=<name>, or substituted by the test harness.
GRANT CONNECT, CREATE ON DATABASE :"dbname" TO wassup_owner;
GRANT CONNECT ON DATABASE :"dbname" TO wassup_migrator, app_voice, app_core, app_ops, wassup_backup;
-- No role needs temporary tables. Without TEMPORARY a session can't create pg_temp objects that
-- shadow real tables inside a SECURITY DEFINER function (they also pin pg_temp last; see 0004).
REVOKE TEMPORARY ON DATABASE :"dbname" FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO wassup_owner;
-- Postgres requires a function's owner to hold CREATE on its schema. wassup_resolver is NOLOGIN
-- (as is wassup_auditor) and only the owner role can SET ROLE to them, so this opens nothing to the
-- app roles.
GRANT USAGE, CREATE ON SCHEMA public TO wassup_resolver, wassup_auditor;
GRANT USAGE ON SCHEMA public TO app_voice, app_core, app_ops, wassup_backup;
