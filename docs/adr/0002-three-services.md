# ADR 0002 — Three services and one shared library, no more

**Status:** Accepted

## Context
The legacy system runs the phone webhook, the voice tools and the staff dashboard in one process,
so a dashboard bug or deploy can take the phone line down. Splitting into many micro-services would
add operational cost a small team can't carry.

## Decision
| Service | Why separate | Target |
|---|---|---|
| `voice-gateway` | The phone path must never share a process, deploy or DB pool with dashboard load | 99.9% ingestion, tool p95 < 800 ms, hard stop 1.5 s |
| `core-api` | Staff traffic and auth have a different shape | 99.5% |
| `ops-worker` | Scheduled/background work must never block requests | every job heartbeats externally |

`libs/wassup_core` holds shared code; nothing is copied by hand between services. Notifications,
audit, analytics and billing stay modules until measured load justifies a split.

## Consequences
Three deployables, one image definition, one migration history. Services never call each other on
the hot path; they share Postgres (each with its own role) and the outbox (ADR 0005).
