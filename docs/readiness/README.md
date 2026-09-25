# Production-readiness gate

Before a release takes any real clinic, and again for any release that changes auth, tenancy,
migrations or the voice path, every check below is run and its result recorded in a dated file in
this directory (`YYYY-MM-DD-vX.Y.Z.md`), with the command used and the evidence. A check that
cannot be run is recorded as **not run**, with the reason, and the release does not proceed.

| # | Check | How | Pass when |
|---|---|---|---|
| 1 | Tenancy | `uv run pytest tests/tenancy` as the app roles | all green; every table FORCE RLS; no BYPASSRLS; definer functions on the allowlist |
| 2 | Cross-clinic access | core-api tests + a manual probe on staging with two clinics | another clinic's call, list, events and analytics are 404 |
| 3 | Load | k6 at 3× projected peak against staging voice-gateway (webhook + tools) | tool p95 < 800 ms, no dropped webhooks, no 5xx except deliberate |
| 4 | Degraded mode | pause staging Postgres for 2 minutes during synthetic traffic | tools answer their fallback within 1.5 s; webhook returns 503; after resume, replay leaves zero missing calls (`/health/freshness` green) |
| 5 | Dead worker | stop ops-worker for 10 minutes | heartbeat alert fires (Better Stack); `/health/canary` reports stale |
| 6 | Alerting path | force one canary failure and one dead-letter | an email reaches ops; weekly synthetic alert configured |
| 7 | Restore drill | restore the latest encrypted dump into a temporary environment | app boots on it; per-table row counts match the source |
| 8 | Migrations | a deliberately failing migration on a scratch DB | previous revision intact; concurrent deploy test passes |
| 9 | Security | gitleaks, semgrep, zizmor; manual: 401 without token, 413 over body caps, no CORS for a foreign origin, `/docs` 404 in production, web CSP headers present on 200 and 404 | all clean |
| 10 | Observability | a request with a synthetic caller name and number; read the logs | no personal data in logs; request/clinic/call ids present; Sentry (if enabled) has `send_default_pii=False` and no request bodies |
| 11 | Heartbeats | every scheduled job has a heartbeat URL configured in production | Better Stack shows each job green |
| 12 | Runbooks | go-live, release, phone-line-down, outbox-dead-letter, replay-exhausted, quarantine, legacy-import, restore-drill | each followed once end to end on staging |
| 13 | Clock changes | tests at Sydney DST start (4 Oct 2026) and for a Brisbane clinic | local date/hour correct; canary claims one run per local day |
| 14 | Rollback | deploy the previous tag to staging, then the current one again | both succeed; `/health` tree matches each time |

Owner-only prerequisites that gate items 5, 6, 7 and 11: Better Stack account and heartbeat URLs,
Resend key, an object-store bucket for backups.
