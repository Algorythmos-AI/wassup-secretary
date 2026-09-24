# Runbook: standing up staging and production, and cutting a clinic over

Everything here is an owner action: creating infrastructure, setting secrets, or changing live
telephony. **No secret values are ever written in this repository.** They go into the platform's
environment settings.

## 0. Prerequisites

| Item | Why | Status |
|---|---|---|
| Actions budget, or keep this repo public | CI and image builds | public for now |
| `PUBLISH_IMAGES=true` repository variable | enables `release.yml` to push images to GHCR | off |
| Resend account and a verified sending domain | urgent-message and line-check alert emails | needed |
| Telephony API key for the monitor (Twilio console → API keys) | account status, balance and number checks | needed |
| Better Stack (or similar) | external monitors and job heartbeats | needed |
| A separate Retell **workspace** for staging | staging keys can never touch production agents | needed |

## 1. Database (once per environment)

1. Create a Postgres 15+ database for the environment (for example a Railway Postgres service).
2. As the database admin, run `db/roles.sql`, then `db/grant_database.sql` (pass `-v dbname=<name>`).
   Both are idempotent: re-run them whenever a release changes them (migrations check for the roles
   they need and stop with a clear message if one is missing):
   ```bash
   psql "$ADMIN_URL" -f db/roles.sql
   psql "$ADMIN_URL" -v dbname=wassup -f db/grant_database.sql
   ```
3. Give each login role a **long random password**, typed at the prompt and never pasted into files:
   `\password wassup_migrator`, `\password app_voice`, `\password app_core`, `\password app_ops`.
4. Build one connection URL per role. Each service gets **only its own** URL.

## 2. Services (one per image; same image in staging and production)

Each service's build and deploy settings are code, in `deploy/railway/<service>.json`, and CI
validates them against Railway's schema. For each of the three services, set the following in the
Railway service settings:
- **Source:** this repository, branch `integration` for staging and `main` for production.
- **Config file path:** `/deploy/railway/<service>.json`. This supplies the Dockerfile, the watch
  paths, the `/health` check, the restart policy, the replicas, 35 s of draining, and for
  ops-worker the migration pre-deploy step.
- **Variable `SERVICE=<service>`:** Railway passes it to the Dockerfile's `ARG SERVICE`, which
  selects the package and the start module.
- **Private networking on**, and no public domain for ops-worker unless monitors need its
  `/health/*` endpoints. Those endpoints expose counts only.

| Service | Image | Start | Pre-deploy | Key environment |
|---|---|---|---|---|
| voice-gateway | `wassup-secretary-voice-gateway` | default CMD | — | `WASSUP_DATABASE_URL` (app_voice), `WASSUP_RETELL_API_KEY`, `WASSUP_AI_LINE_NUMBERS`, `WASSUP_ENVIRONMENT` |
| core-api | `wassup-secretary-core-api` | default CMD | — | `WASSUP_DATABASE_URL` (app_core), `WASSUP_FIREBASE_PROJECT_ID`, `WASSUP_CORS_ORIGINS`, `WASSUP_ENVIRONMENT` |
| ops-worker | `wassup-secretary-ops-worker` | default CMD | `alembic -c db/alembic.ini upgrade head` with `WASSUP_MIGRATION_DATABASE_URL` (migrator) | `WASSUP_DATABASE_URL` (app_ops), `WASSUP_RESEND_API_KEY`, `WASSUP_ALERT_EMAIL_FROM`, `WASSUP_OPS_ALERT_EMAILS`, `WASSUP_RETELL_API_KEY`, `WASSUP_AI_LINE_NUMBERS`, `WASSUP_VOICE_GATEWAY_URL` (private-network URL, enables replay), `WASSUP_TWILIO_ACCOUNT_SID` + `WASSUP_TWILIO_API_KEY_SID` + `WASSUP_TWILIO_API_KEY_SECRET` (telephony monitor; an API key, never the auth token), `WASSUP_TELEPHONY_MIN_BALANCE`, `WASSUP_VOICE_WEBHOOK_URL` (the voice-gateway webhook every published agent version must post to), heartbeat URLs |

- Set `WASSUP_ENVIRONMENT` to `staging` or `production`. In these environments the API docs are off and test sign-in is refused. If it is not set, it defaults to `production`, which fails closed.
- Keep the database on the platform's **private network**, with its public TCP proxy off.
- Health checks: `GET /health` on every service. Deploy verification: `/health` must report the expected `tree`.

## 3. Onboard a clinic (data rows, as the owner role, in one transaction)

```sql
SET ROLE wassup_owner;
SELECT set_config('app.clinic_ids', '{<new-clinic-uuid>}', true);
INSERT INTO organizations (id, name) VALUES (...);
INSERT INTO clinics (id, organization_id, slug, name, state, timezone, alert_contacts, status)
  VALUES ('<new-clinic-uuid>', ..., 'nsw', 'Australia/Sydney', '["alerts@clinic.example"]', 'active');
INSERT INTO clinic_voice_agents (clinic_id, agent_id, agent_version, environment) VALUES (...);
INSERT INTO clinic_phone_numbers (clinic_id, e164) VALUES (...);
```

Then add staff: a `staff_users` row keyed by their Firebase uid, plus `clinic_memberships` with a role.

## 4. Point the clinic's voice agent at the new platform (staging first)

1. **Retell workspace for the environment:** create a new agent draft and set:
   - webhook: `https://<voice-gateway>/v1/retell/webhook`
   - each tool: `https://<voice-gateway>/v1/retell/tools/<clinic-slug>/<tool>`, with `timeout_ms` about 3000 and `max_retry` 0.
   - the prompt must follow the [voice tool contract](../voice-tools.md): `ok: false` means **not saved**, and the agent must say so.
2. **Publish** the draft, then **rebind the number** to the new published version with the operator CLI:
   ```bash
   export RETELL_API_KEY=...            # from the platform's secret store; never in files
   uv run wassup voice export --out ~/wassup-backup/$(date +%F)   # backup first (private files)
   uv run wassup voice rebind +61XXXXXXXXX --agent agent_… --version N            # dry run
   uv run wassup voice rebind +61XXXXXXXXX --agent agent_… --version N --apply    # verified
   ```
   The CLI refuses drafts and "latest", re-reads the number to verify, and records the previous
   binding locally.
3. **Record the binding.** Set `clinic_voice_agents.agent_version` to the published version you bound. `/health/voice-config` then pages if anyone rebinds the number, edits it onto a draft, or changes the webhook.
4. **Test call.** The call must appear in `calls` with the right `clinic_id`, and `/health/freshness` on ops-worker must say `ok`.
5. **Rollback** takes seconds: `uv run wassup voice rollback +61XXXXXXXXX --apply` restores the recorded previous binding. Then update `agent_version` to match.

## 5. Monitors (Better Stack)
- HTTP monitors on each service's `/health`.
- Alert on non-2xx from ops-worker `/health/freshness` (ingestion gap).
- Alert on non-2xx from ops-worker `/health/canary`, once the line check is enabled.
- Alert on non-2xx from ops-worker `/health/outbox`, which covers dead letters, overdue alerts and repeated failures. See the [runbook](outbox-dead-letter.md).
- Alert on non-2xx from ops-worker `/health/replay`, which covers webhook events or tool requests that couldn't be processed. See the [runbook](replay-exhausted.md).
- Alert on non-2xx from ops-worker `/health/telephony`: the account is suspended or closed, the balance is below the floor, a line number is missing from the account, or the check is stale. This is the September 2026 outage, caught in minutes.
- Alert on non-2xx from ops-worker `/health/voice-config`, which fires when a clinic number is not bound to its clinic's agent, is on a draft or "latest" version, is not on the pinned version, or is on a version whose webhook isn't ours.
- Alert on non-2xx from ops-worker `/health/quarantine`, which fires while signed calls that couldn't be matched to a clinic await a decision. See the [runbook](quarantine.md).
- Heartbeat monitors: `WASSUP_OUTBOX_HEARTBEAT_URL`, `WASSUP_CANARY_HEARTBEAT_URL`, `WASSUP_REPLAY_HEARTBEAT_URL`, `WASSUP_TELEPHONY_HEARTBEAT_URL`, `WASSUP_VOICE_CONFIG_HEARTBEAT_URL` and `WASSUP_QUARANTINE_HEARTBEAT_URL`.
- A service's own `/health` answers 503 `not_ready` until the database schema it needs is in place. Railway therefore holds a deploy until ops-worker's pre-deploy migration has run.
- These endpoints are unauthenticated but cached and single-flight: at most one database or provider query per probe every 15–60 s.

## 6. Daily line check
The line check needs the telephony trunk to allow **outbound calls to Australian numbers**: termination credentials for the voice provider, and geographic permissions limited to Australia. Then set:
- `WASSUP_CANARY_ENABLED=true`
- `WASSUP_CANARY_AGENT_ID` / `WASSUP_CANARY_AGENT_VERSION` (a one-line "line check" agent)

## 7. Legacy history
Each clinic's history is imported from the legacy system by a one-off ETL kept in the **private** legacy repository (ADR 0009). It is verified by row counts and checksums before that clinic's dashboard switches to core-api.
