# Changelog

All notable changes are recorded here. Versions follow SemVer. One version covers the whole monorepo.

## [Unreleased]
- Repository bootstrap: licence, notice, agent rules, security policy.
- uv workspace: `wassup_core` (settings, allowlist log redaction, RFC 9457 errors, per-route body limits, app factory) and three service skeletons with `/health`; CI (lint, types, tests with Postgres, image build + smoke, `ci-gate`), security scans, PR-title check.
- Database foundation: role model (`db/roles.sql`), Alembic migration `0001` with every tenant table under ENABLE + FORCE row-level security, three narrowly granted SECURITY DEFINER resolvers owned by a read-only `wassup_resolver` role, least-privilege grants per service, append-only audit log; 36 tenancy tests run as the real app roles against Postgres in CI.
- voice-gateway webhook ingestion: signature verified on raw bytes (current or previous key, 5-minute replay window, constant time), raw event stored before processing, clinic resolved from agent and dialled number (mismatch → quarantine), call upsert that never erases earlier facts, outbox event in the same transaction, synthetic line-check calls never stored, 503 on database outage so the provider retries.
- voice-gateway tool calls `/v1/retell/tools/{clinic_slug}/{tool}`: exactly-once via a claimed `tool_invocations` row in the same transaction (safe under concurrent retries), clinic slug must match the signed agent + dialled number, hard time budget with per-tool fallbacks, synthetic calls are no-ops. Tools: `capture_message` (urgent → `message.urgent` outbox event), `create_promise`, privacy-first `lookup_patient` (exact match, opaque ref, deceased = no match, 2 per call).
