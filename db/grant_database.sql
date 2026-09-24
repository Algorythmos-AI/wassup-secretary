-- Run once per database (as the database owner / admin) after roles.sql.
-- :"dbname" is supplied by psql -v dbname=<name>, or substituted by the test harness.
GRANT CONNECT, CREATE ON DATABASE :"dbname" TO wassup_owner;
GRANT CONNECT ON DATABASE :"dbname" TO wassup_migrator, app_voice, app_core, app_ops;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO wassup_owner;
-- Postgres requires a function's owner to hold CREATE on its schema. wassup_resolver is NOLOGIN
-- and only the owner role can SET ROLE to it, so this opens nothing to the app roles.
GRANT USAGE, CREATE ON SCHEMA public TO wassup_resolver;
GRANT USAGE ON SCHEMA public TO app_voice, app_core, app_ops;
