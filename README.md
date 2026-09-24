# WASSUP Secretary

Multi-clinic AI phone receptionist platform by Algorythmos: a voice agent answers clinic calls,
and staff handle the resulting calls, messages and callbacks in a dashboard.

> **Proprietary.** Temporarily public for CI compute only — see [`NOTICE.md`](NOTICE.md) and [`LICENSE`](LICENSE).

## Layout
| Path | Purpose |
|---|---|
| `services/voice-gateway` | Voice-provider webhooks and tool calls (signed, idempotent, clinic-scoped) |
| `services/core-api` | Staff/dashboard API `/v1` |
| `services/ops-worker` | Reconciliation, line checks, alerts, backups |
| `libs/wassup_core` | Shared domain, database, tenancy, auth, logging |
| `db/` | Alembic migrations (single schema owner) |
| `docs/` | Architecture, ADRs, runbooks |

## Develop
```bash
make bootstrap   # uv sync + pre-commit hooks
make test-fast   # unit tests, no database
make test        # full suite against a local Postgres (DATABASE_URL)
make ci          # everything CI runs
```

Branching, data rules and the definition of done: [`AGENTS.md`](AGENTS.md).
