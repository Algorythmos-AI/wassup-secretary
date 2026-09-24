# ADR 0011: Watch external configuration continuously; stream live events by polling

**Status:** Accepted

## Context
The September 2026 outage lasted 13 days. The telephony account was suspended, and nothing inside our system could see that. Several other external settings silently break call capture in the same way:
- a number bound to a draft or the wrong agent;
- a changed webhook URL;
- a released number.

Separately, dashboards need live updates. The legacy broadcaster had no clinic filter.

## Decision
- **External-state monitors run in ops-worker.**
  - Telephony (every 5 min): account status, balance floor, number ownership.
  - Voice configuration (every 15 min): each number is bound to its clinic's pinned, published agent version, and that version posts to our webhook.
  - A monitor alerts ops when it changes to failing, then periodically while it stays failing.
  - Its `/health/*` endpoint serves the last result **from memory**. An unauthenticated endpoint never calls a provider.
  - It turns red when stale, so a dead job never looks healthy.
  - Credentials are read-only API keys. Nothing a provider returns (for example an auth token in an account payload) is logged or stored.
- **Live events are polled, not pushed by `LISTEN/NOTIFY`.**
  - Each SSE stream polls the clinic's outbox by `(clinic_id, id)` every 2 s.
  - Streams run under row-level security, re-check membership every 5 minutes, and end when the viewer's token expires.
  - `NOTIFY` (ADR 0005) remains an optional latency optimisation. It would cost a dedicated long-lived connection per replica and extra failure modes, for about 2 s saved.

## Consequences
- DB load is about 0.5 queries per second per open screen, which is trivial up to hundreds of screens. Revisit, with NOTIFY or a shared per-clinic poller, if connection or query budgets tighten (see the plan's scale envelope).
- Each monitor needs its provider credentials configured. Until they are, its health endpoint reports `unconfigured` (503) rather than green.
