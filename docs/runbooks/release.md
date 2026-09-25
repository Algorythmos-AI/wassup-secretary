# Runbook: releasing to production

Production only ever runs a **release**: a `v*` tag on `main`. Nothing else can reach it:
`scripts/deploy-railway.sh production …` refuses an untagged or off-main commit, and the
`deploy-production` job in `.github/workflows/release.yml` checks the same things plus green CI
and a CHANGELOG entry before it deploys.

## Before cutting a release

1. **Staging soak, 24 hours.** The commit to be released has run on staging for a day with
   synthetic traffic (signed webhooks and tool calls) and at least one live-events client open.
   Every `/health/*` on ops-worker that has a configured provider is green; the others say why.
2. **CHANGELOG.** Move the `[Unreleased]` items under `## [X.Y.Z] - YYYY-MM-DD`. One version
   covers the whole monorepo.
3. **Readiness gate** (first release, and any release that changes auth, tenancy, migrations or
   the voice path): the checks in `docs/readiness/` are re-run and recorded.

## Cut it

```bash
git checkout integration && git pull --ff-only
gh pr create --base main --head integration --title "release: vX.Y.Z" --body "See CHANGELOG."
# wait for CI, then merge as a MERGE COMMIT (main keeps integration's history)
gh pr merge --merge
git checkout main && git pull --ff-only
git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
```

The tag push starts `deploy-production`. It needs the repository secret `RAILWAY_TOKEN` (a Railway
project token scoped to `production`) and the variables `PROD_VOICE_GATEWAY_URL`, `PROD_CORE_API_URL`,
`PROD_OPS_WORKER_URL`, `PROD_WEB_URL` for verification. Until the token exists, the same deploy runs
from a clean checkout of the tag:

```bash
git checkout vX.Y.Z
scripts/deploy-railway.sh production db-admin ops-worker voice-gateway core-api web
```

Order matters and is fixed: `db-admin` (roles), `ops-worker` (migrations, then serve),
`voice-gateway`, `core-api`, `web`. Each waits for the previous service's health check.

## Verify

- Every service's `/health` reports `"tree"` equal to `git rev-parse vX.Y.Z^{tree}`; `web/version.json`
  reports the same build.
- ops-worker `/health/outbox`, `/health/replay`, `/health/quarantine` are green; `/health/voice-config`
  and `/health/telephony` are green once their providers are configured.
- One synthetic signed webhook to voice-gateway's production URL (the line-check agent's call, or the
  seeded synthetic clinic in staging only; production has no synthetic clinic) lands within 10 s.

## Rollback

Re-run `release.yml` with `tag` = the previous release (Actions → release → Run workflow), or from a
checkout of that tag: `scripts/deploy-railway.sh production ops-worker voice-gateway core-api web`.
Migrations are forward-only; a release whose migration cannot be undone is rolled back by deploying
the previous images, which tolerate the newer schema (additive migrations only; see AGENTS.md).

## Creating the production environment (once)

Railway can duplicate an environment. From the linked project:

```bash
railway environment new production --duplicate staging \
  --service-config db-admin variables.WASSUP_ENVIRONMENT.value production \
  --service-config ops-worker variables.WASSUP_ENVIRONMENT.value production \
  --service-config voice-gateway variables.WASSUP_ENVIRONMENT.value production \
  --service-config core-api variables.WASSUP_ENVIRONMENT.value production \
  --service-config web variables.WASSUP_ENVIRONMENT.value production
```

Then, in the new environment:
1. Regenerate every role password: set each `WASSUP_PASSWORD_*` on `db-admin` to `${{secret(40)}}`
   again (duplication copies staging's values; production must have its own).
2. `WASSUP_ROLE=bootstrap` on `db-admin` for the first deploy; `report` afterwards.
3. Confirm the new Postgres is **empty** (`report` prints `clinics: 0`). Production is never seeded.
4. Public domains for voice-gateway, core-api and web; set `WASSUP_CORS_ORIGINS` on core-api and
   `VITE_API_BASE` on web to the production URLs; set `WASSUP_VOICE_WEBHOOK_URL` on ops-worker.
5. Owner-only secrets: a production `WASSUP_RETELL_API_KEY` (separate workspace), Resend, Twilio
   monitor key, heartbeat URLs. Nothing from staging's copies is trusted for production.
