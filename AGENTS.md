# AGENTS.md — rules for humans and AI agents in `wassup-secretary`

Read the org rules first: `Algorythmos-AI/.github-private` → `AGENTS.md` and `catalog.yaml`.
This file adds what is specific to this repository. The org rules win where they conflict,
except that the **Data** and **Public repository** rules below are never relaxed.

## What this is
WASSUP Secretary is a multi-clinic AI phone receptionist platform: a voice agent answers
clinic calls, and staff work the resulting calls, messages and callbacks in a dashboard.
It is three Python services, one shared library, a web app and a set of operator tools:

| Path | Role |
|---|---|
| `services/voice-gateway` | The only service the voice provider (Retell) talks to: signed webhooks and tool calls. Small, stateless, highest availability. Classifies analysed calls with the clinic's active rules. |
| `services/core-api` | Staff and dashboard API (`/v1`): sign-in, clinic membership and team management, calls and workflow, analytics, usage, live events. |
| `services/ops-worker` | Scheduled and background jobs: outbox delivery and alerts, replay, line checks, telephony and voice-config monitors, usage rollup, retention. (Backups are planned, not built: see the completion plan.) |
| `apps/web` | The reception dashboard. Types are generated from `contracts/core-api.openapi.json`; CI fails if they drift. |
| `libs/wassup_core` | Shared settings, DB access and tenancy, logging, the classification engine (`classify.py`). No hand-copied code between services. |
| `db/` | Alembic migrations (the only owner of the schema) and the one-shot tools `db-admin` runs: `bootstrap.py`, `seed_synthetic.py`, `report.py`, `import_legacy.py`, `classifier_rules.py`. |
| `tools/wassup-cli` | Voice-agent bindings: export, rebind, rollback. |

The architecture and its decisions live in `docs/architecture.md` and `docs/adr/`.

## Branches and releases
- `integration` is the default branch. Branch from it and open a PR into it. Staging runs `integration`.
- `main` is production. It only changes through a release PR `integration → main` (a merge commit), and production is deployed only from a `v*` tag on `main` (`docs/runbooks/release.md`).
- PR titles use Conventional Commits: `feat(core-api): …`, `fix(voice-gateway): …`.
- Never push directly to `integration` or `main`. Never force-push shared branches.

## Definition of done
`make ci` is green locally. The same checks run in CI, and the required check is `ci-gate`.
New behaviour has tests. A migration comes with a tenancy test for any new tenant table.
Anything touching tenancy, auth, alerts, migrations, the import or the classifier gets an
independent adversarial review before merge, and a test that fails without the fix. A production
change comes with a rehearsed rollback.

## Migrations
- Forward-only, one transaction per revision (`db/migrations/env.py`), always `SET ROLE wassup_owner`.
- SQL is fixed literal text. There is no string-built SQL anywhere, and no `noqa: S608`.
- **Bump the readiness probe.** If a service's code needs a new schema object, change that service's `SCHEMA_PROBE` in the same PR. The service's `/health` then stays 503 until the migration has run, and the deploy won't go live early.
- **Big tables.** Once a table the phone path writes to (`calls`, `outbox_events`, `retell_events_raw`, `tool_requests_raw`, `audit_log`) holds real volume, build indexes with `CREATE INDEX CONCURRENTLY` inside `op.get_context().autocommit_block()`. Use expand, then backfill, then contract for column changes. A plain `CREATE INDEX` blocks writers while it runs.
- **Definer functions** pin `search_path = pg_catalog, public, pg_temp` and are added to `DEFINER_ALLOWLIST` in `tests/tenancy/test_structure.py` with their owner role.

## Data (never relaxed)
- **No real patient, caller, staff or clinic data anywhere:** not in code, fixtures, logs,
  commit messages or PR text. Use obviously synthetic values: `+61400000xxx` numbers,
  `*.example.test` emails, names like "Test Patient".
- Every tenant table has `clinic_id`, `ENABLE` + `FORCE ROW LEVEL SECURITY`, and is covered by
  `tests/tenancy`. Tenant queries go through repositories that set the clinic context.
- Logs use the allowlist redactor in `wassup_core.logging`. Never log request bodies, transcripts,
  names, dates of birth or phone numbers.

## Public repository (while `visibility: public`)
- No secrets. Configuration comes from environment variables. `gitleaks` runs in pre-commit and CI.
- No voice-agent prompts, production clinic config, phone numbers, agent IDs, or clinic
  classification vocabulary (rules are loaded into the database by the owner, never committed).
- Nothing copied from any private Algorythmos repository, including the legacy
  `wassup-call-dashboard`. Re-implement from the design, not from the old code.

## Stop and ask a human first
Production deploys or data changes, destructive migrations, visibility/ruleset/org settings,
vendor billing, creating or rotating credentials, and anything touching real clinic config.
