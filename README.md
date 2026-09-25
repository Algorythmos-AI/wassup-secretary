# WASSUP Secretary

Multi-clinic AI phone receptionist platform by Algorythmos: a voice agent answers clinic calls,
and staff handle the resulting calls, messages and callbacks in a dashboard.

> **Proprietary.** Temporarily public for CI compute only — see [`NOTICE.md`](NOTICE.md) and [`LICENSE`](LICENSE).

## Layout
| Path | Purpose |
|---|---|
| `services/voice-gateway` | Voice-provider webhooks and tool calls (signed, idempotent, clinic-scoped); classifies each analysed call with the clinic's rules |
| `services/core-api` | Staff/dashboard API `/v1`: sign-in, calls, workflow, analytics, usage, live events, team |
| `services/ops-worker` | Outbox delivery and alerts, replay, line checks, telephony and voice-config monitors, usage rollup, retention |
| `apps/web` | The reception dashboard (React + TypeScript, typed from `contracts/core-api.openapi.json`): inbox, analytics, usage, team, office TV |
| `libs/wassup_core` | Shared settings, database access and tenancy, logging, the classification engine |
| `db/` | Alembic migrations (the only schema owner) and the one-shot operator tools run by `db-admin`: role bootstrap, synthetic seed, counts report, legacy import, classifier rules |
| `tools/wassup-cli` | Operator CLI for voice-agent bindings (export, rebind, rollback) |
| `deploy/` | Container entrypoint and Railway service configs |
| `docs/` | Architecture, ADRs, runbooks, the readiness gate |

Each clinic's data is isolated by forced row-level security; each service connects as its own
least-privilege database role. Which words make a call urgent for a clinic is that clinic's own
data (`docs/runbooks/classifier-rules.md`), never code in this repository.

## Develop
```bash
make bootstrap   # uv sync + pre-commit hooks
make test-fast   # unit tests, no database
make test        # full suite against a local Postgres (TEST_DATABASE_ADMIN_URL)
make ci          # everything CI runs
cd apps/web && npm ci && npm run dev   # the dashboard (VITE_API_BASE, VITE_AUTH_MODE=test locally)
```

## Ship
`integration` is deployed to staging by exact commit (`scripts/deploy-railway.sh staging …`).
Production only ever runs a release: a `v*` tag on `main`, with green CI and a CHANGELOG entry,
deployed in a fixed order and verified by `/health` (`docs/runbooks/release.md`). The checks a
release must pass before it takes a real clinic are in `docs/readiness/`.

Branching, data rules and the definition of done: [`AGENTS.md`](AGENTS.md).
