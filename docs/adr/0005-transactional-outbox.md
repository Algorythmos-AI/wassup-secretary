# ADR 0005 — Transactional outbox; NOTIFY is only a wake-up hint

**Status:** Accepted

## Decision
Side effects (alerts, live dashboard updates) are rows in `outbox_events`, written in the same
transaction as the data change, with a unique dedupe key. ops-worker claims them with
`FOR UPDATE SKIP LOCKED`, retries with a dead-letter state, and polls every few seconds as a
fallback. `LISTEN/NOTIFY` carries only the event id (never data) to wake consumers early; core-api
re-reads the event under the subscriber's own clinic context before pushing it over SSE.

## Consequences
No lost or duplicated notifications across restarts or multiple replicas; no personal data in NOTIFY.
