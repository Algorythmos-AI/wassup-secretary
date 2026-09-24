# Architecture

WASSUP Secretary answers clinic phone calls with a voice agent and gives clinic staff a live
dashboard of calls, messages and callbacks. It is multi-clinic from the first line of schema.

```
Patient ─► Clinic phone system (conditional divert, tested fallback)
        ─► Telephony provider number ─► SIP trunk ─► Voice provider agent (one per clinic,
           pinned to a published version)
              │  signed webhooks + tool calls (per-agent URLs)
┌─────────────▼─────────────────────────────────────────────────────────────────┐
│ voice-gateway  verify signature on raw bytes → store raw event → resolve clinic   │
│                (agent AND dialled number must agree) → write call/messages +      │
│                outbox row in one transaction. Tools: idempotent, 1.5 s budget.    │
│ core-api       staff API /v1: calls, workflow, analytics, SSE, memberships        │
│ ops-worker     outbox consumer, reconciliation, line-check canary, telephony and  │
│                billing monitors, alerts, usage, backups; runs migrations          │
│ Postgres 15    one role per service, FORCE row-level security on every tenant    │
│                table, raw event log, outbox                                       │
└──────────────────────────────────────────────────────────────────────────────┘
      ▲ HTTPS, staff identity token → clinic membership and role (database)
Web dashboard (React) — staging and production are separate projects
```

## Principles
1. **The phone path is sacred.** voice-gateway is small, stateless and isolated from dashboard load.
2. **Isolation is enforced by the database**, not by remembering a `WHERE` clause (ADR 0003).
3. **Store first, process second.** Raw provider events are persisted before processing (ADR 0004).
4. **Every side effect is idempotent** and flows through the outbox (ADR 0005).
5. **Outages are detected, not reported by clinics.** A daily synthetic call rings each line; an
   ingestion-gap check compares the provider's call log with ours; every job heartbeats externally.
6. **Deny by default:** logs use an allowlist redactor, docs are off in production, errors are
   RFC 9457 bodies with no internal detail, request bodies have per-route limits.

## Repository map
| Path | Contents |
|---|---|
| `libs/wassup_core` | settings, logging, HTTP plumbing, app factory; database and tenancy helpers |
| `services/*` | one FastAPI app per service |
| `db/roles.sql`, `db/grant_database.sql` | one-time cluster/database bootstrap (admin) |
| `db/migrations` | Alembic, forward-only |
| `tests/tenancy` | isolation and catalog-structure tests (run as the real roles) |
| `docs/adr` | decisions |

## Delivery status (25 Sep 2026)
| Phase | Scope | State |
|---|---|---|
| 1 | Repo, CI, rulesets, workspace, service skeletons, Railway config-as-code | done |
| 2 | Database roles, tenancy schema, isolation tests, tenancy-aware DB layer, hash-chained audit | done |
| 3 | ops-worker: outbox and urgent alerts, replay, retention; monitors for the line-check canary, ingestion gap, telephony account, voice configuration, quarantine, outbox and replay | done (canary awaits outbound telephony; monitors await provider keys) |
| 4 | voice-gateway: signed ingestion, idempotent tools with budgets and truthful fallbacks; `wassup` CLI for cutover and rollback | done. Per-clinic cutover is pending infrastructure |
| 5 | core-api: staff auth, calls API, workflow, analytics summary, live events (SSE) | API done. The dashboard UI is still served by the legacy app. The domain classifier (priority and reception action) waits for the repo to be private again (ADR 0007) |
| 6 | Staging and production infrastructure, clinic cutover, legacy import, decommission | owner actions (see `docs/runbooks/go-live.md`) |
