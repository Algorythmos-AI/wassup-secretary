# Changelog

All notable changes are recorded here. Versions follow SemVer. One version covers the whole monorepo.

## [Unreleased]
- Repository bootstrap: licence, notice, agent rules, security policy.
- uv workspace: `wassup_core` (settings, allowlist log redaction, RFC 9457 errors, per-route body limits, app factory) and three service skeletons with `/health`; CI (lint, types, tests with Postgres, image build + smoke, `ci-gate`), security scans, PR-title check.
- Database foundation: role model (`db/roles.sql`), Alembic migration `0001` with every tenant table under ENABLE + FORCE row-level security, three narrowly granted SECURITY DEFINER resolvers owned by a read-only `wassup_resolver` role, least-privilege grants per service, append-only audit log; 36 tenancy tests run as the real app roles against Postgres in CI.
- voice-gateway webhook ingestion: signature verified on raw bytes (current or previous key, 5-minute replay window, constant time), raw event stored before processing, clinic resolved from agent and dialled number (mismatch → quarantine), call upsert that never erases earlier facts, outbox event in the same transaction, synthetic line-check calls never stored, 503 on database outage so the provider retries.
- voice-gateway tool calls `/v1/retell/tools/{clinic_slug}/{tool}`: exactly-once via a claimed `tool_invocations` row in the same transaction (safe under concurrent retries), clinic slug must match the signed agent + dialled number, hard time budget with per-tool fallbacks, synthetic calls are no-ops. Tools: `capture_message` (urgent → `message.urgent` outbox event), `create_promise`, privacy-first `lookup_patient` (exact match, opaque ref, deceased = no match, 2 per call).
- ops-worker: transactional-outbox consumer (`FOR UPDATE SKIP LOCKED`, 5-minute leases so a crashed worker's events are reclaimed, exponential backoff, dead-letter after 8 attempts); urgent messages emailed to the clinic's alert contacts via Resend with per-event delivery records (retries never resend); in-process scheduler with optional external heartbeat; migration `0002` (line-check runs table, alert-delivery grants).
- Outage detectors in ops-worker: daily line-check canary (claimed per line per local day, DST-aware; placement failure or no receipt within 15 min emails ops once; voice-gateway records receipts of synthetic calls), detect-only ingestion-gap reconciler, and monitor endpoints `/health/canary` (503 on failing or stale — a dead scheduler is an outage) and `/health/freshness` (503 only on a proven gap). Runbook `docs/runbooks/phone-line-down.md`.
- core-api `/v1`: Firebase ID-token verification (RS256 against Google's rotating keys; audience, issuer, expiry, verified email; test mode refused outside local/test), staff resolved through the new `staff_memberships` resolver, `/v1/me`, clinic call list with keyset pagination, audited call detail, and workflow actions with `Idempotency-Key` + `If-Match` optimistic locking and role checks (another clinic's data is always 404). Migration `0003` removes `app_core`'s read access to `staff_users` (it had no clinic scoping).
- Security review fixes: every SECURITY DEFINER function pins `pg_temp` last and login roles lose TEMPORARY (a temporary view could forge clinic memberships); unused `staff_clinic_ids` dropped; audit log hash-chained per clinic by a trigger owned by the new NOLOGIN `wassup_auditor` role (gap-free under concurrency, tampering detectable); call-list reads audited with the call ids shown; `Idempotency-Key` bound to one call and request (reuse → 422; replay returns the original answer); exceptions logged as type and location only — never their message — across structlog and library loggers, and SQLAlchemy errors never include bound values; malformed signature headers and cursors are 401/400 instead of 500; `WASSUP_ENVIRONMENT` defaults to `production` (fails closed). Migration `0004`; re-run `db/roles.sql` and `db/grant_database.sql` first.
- Reliability review fixes:
  - **Migrations** are transactional. Each revision commits atomically with its version stamp; autocommit connections are refused; a session advisory lock serialises concurrent deploys (proved with three parallel CLI runs).
  - **Tool requests** are stored raw before write tools run (lookups never are). Write-tool fallbacks now say `ok: false`: the agent never claims an unsaved message was passed on (`docs/voice-tools.md`).
  - **Replay:** ops-worker replays unfinished webhook events and tool requests through voice-gateway, signed like Retell and exactly-once, with backoff over about 16 minutes. When replays are exhausted, ops is emailed and `/health/replay` goes red.
  - **Webhook errors:** the webhook answers 503 on transient database errors, now including pool timeouts; bugs are acknowledged and left for replay.
  - **Line-check calls:** a call from one of our numbers counts as a line check only when that exact line check is running (caller ID can be spoofed).
  - **NUL characters** are stripped before storage.
  - **Outbox:**
    - completion is fenced on (status, attempts);
    - a crash-looping event is dead-lettered instead of re-claimed forever;
    - each dead letter emails ops;
    - `/health/outbox` is red on dead letters, overdue events or repeated failures;
    - new `abandoned` status;
    - only our own error codes are stored as `last_error`.
  - **Monitor endpoints** are cached and single-flight.
  - Migration `0005`; runbooks `outbox-dead-letter.md` and `replay-exhausted.md`.
- Deploy readiness: migrations ship in the ops-worker image (run as the pre-deploy step), build-once image publishing to GHCR tagged by source tree (gated behind `PUBLISH_IMAGES`), go-live runbook.
