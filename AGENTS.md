# AGENTS.md — rules for humans and AI agents in `wassup-secretary`

Read the org rules first: `Algorythmos-AI/.github-private` → `AGENTS.md` and `catalog.yaml`.
This file adds what is specific to this repository. The org rules win where they conflict,
except that the **Data** and **Public repository** rules below are never relaxed.

## What this is
WASSUP Secretary is a multi-clinic AI phone receptionist platform: a voice agent answers
clinic calls, and staff work the resulting calls, messages and callbacks in a dashboard.
It is written in Python and has three services plus one shared library:

| Path | Role |
|---|---|
| `services/voice-gateway` | The only service the voice provider (Retell) talks to: signed webhooks and tool calls. Small, stateless, highest availability. |
| `services/core-api` | Staff and dashboard API (`/v1`), authentication and clinic membership. |
| `services/ops-worker` | Scheduled and background jobs: reconciliation, line checks, alerts, backups. |
| `libs/wassup_core` | Shared domain, DB models, tenancy, auth, logging. No hand-copied code between services. |
| `db/` | Alembic migrations. The only owner of the schema. |

The architecture and its decisions live in `docs/architecture.md` and `docs/adr/`.

## Branches and releases
- `integration` is the default branch. Branch from it and open a PR into it.
- `main` is production. It only changes through a release PR `integration → main` (a merge commit).
- PR titles use Conventional Commits: `feat(core-api): …`, `fix(voice-gateway): …`.
- Never push directly to `integration` or `main`. Never force-push shared branches.

## Definition of done
`make ci` is green locally. The same checks run in CI, and the required check is `ci-gate`.
New behaviour has tests. A migration comes with a tenancy test for any new tenant table.

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
- No voice-agent prompts, production clinic config, phone numbers or agent IDs.
- Nothing copied from any private Algorythmos repository, including the legacy
  `wassup-call-dashboard`. Re-implement from the design, not from the old code.

## Stop and ask a human first
Production deploys or data changes, destructive migrations, visibility/ruleset/org settings,
vendor billing, creating or rotating credentials, and anything touching real clinic config.
