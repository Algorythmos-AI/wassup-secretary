# Runbook: outbox dead letter

**Signal:**
- an email "WASSUP outbox dead letter (…)", or
- `GET /health/outbox` on ops-worker returns 503.

The payload reports `dead`, `overdue` and `retrying` counts.

**What it means:** an event (for example an urgent-message alert) failed 8 times, or the worker
crashed while handling it on its last attempt. It is **not** retried until someone decides.
`retrying > 0` means an event has failed at least 3 times in a row and is still being retried. The
usual causes are the email provider or missing alert contacts.

## How decisions are made

The database is on the private network only. Every step below is a run of the one-shot
`db-admin` service (the ops-worker image) with `WASSUP_ROLE=ops` and the variables shown, then
a deploy of `db-admin`; its log is the answer. Changes are a dry run until
`WASSUP_OPS_APPLY=true`; in production an apply also needs `WASSUP_PRODUCTION_ACK` set to the
action's name. Every change is written to the clinic's audit log. Afterwards set
`WASSUP_ROLE=report` again and remove the `WASSUP_OPS_*` variables.

## 1. Find it (ids and error codes only)

`WASSUP_OPS_ACTION=list`. The log lists each dead event: `id`, `clinic_id`, `event_type`,
`attempts`, `last_error`, `created_at`.

## 2. Fix the cause

| `last_error` | Cause | Fix |
|---|---|---|
| `no_alert_contacts` | The clinic has no urgent alert email configured | Add `alert_contacts` for the clinic |
| `message_not_found` | The message row is missing (should not happen) | Investigate before re-queueing |
| `RuntimeError`, `HTTPStatusError`, `ConnectError`, `email_not_configured`, … | Email provider problem | Check the Resend status and the API key |
| `lease_expired_at_max_attempts` | The worker crashed mid-handler every time | Check ops-worker logs and memory, then re-queue |

**For an urgent message, phone the clinic now.** Don't wait for the fix.

## 3. Decide: re-queue or abandon

- Re-queue (after the fix): `WASSUP_OPS_ACTION=requeue-outbox`, `WASSUP_OPS_IDS=<id>[,<id>…]`.
- Abandon (for example, the clinic was phoned instead; record why in the incident log):
  `WASSUP_OPS_ACTION=abandon-outbox`, `WASSUP_OPS_IDS=…`.

Deploy once as a dry run (the log shows the ids that would change), then with
`WASSUP_OPS_APPLY=true` (log ends `committed`). An id that isn't dead-lettered refuses the whole
run. `/health/outbox` goes green once no event is `dead`, overdue or failing repeatedly.
