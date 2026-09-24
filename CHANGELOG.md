# Changelog

All notable changes are recorded here. Versions follow SemVer. One version covers the whole monorepo.

## [Unreleased]
- Repository bootstrap: licence, notice, agent rules, security policy.
- uv workspace: `wassup_core` (settings, allowlist log redaction, RFC 9457 errors, per-route body limits, app factory) and three service skeletons with `/health`; CI (lint, types, tests with Postgres, image build + smoke, `ci-gate`), security scans, PR-title check.
- Database foundation: role model (`db/roles.sql`), Alembic migration `0001` with every tenant table under ENABLE + FORCE row-level security, three narrowly granted SECURITY DEFINER resolvers owned by a read-only `wassup_resolver` role, least-privilege grants per service, append-only audit log; 36 tenancy tests run as the real app roles against Postgres in CI.
