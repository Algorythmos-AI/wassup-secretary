# Runbook: outbox dead letter

**Signal:**
- an email "WASSUP outbox dead letter (…)", or
- `GET /health/outbox` on ops-worker returns 503.

The payload reports `dead`, `overdue` and `retrying` counts.

**What it means:** an event (for example an urgent-message alert) failed 8 times, or the worker
crashed while handling it on its last attempt. It is **not** retried until someone decides.
`retrying > 0` means an event has failed at least 3 times in a row and is still being retried. The
usual causes are the email provider or missing alert contacts.

## 1. Find it (ids and error codes only)
Connect as the database **admin**. It is the only role that row-level security doesn't apply to;
every app role, and even the schema owner, sees nothing without a clinic context. Work inside a
transaction (`BEGIN;`).
```sql
SELECT id, clinic_id, event_type, attempts, last_error, created_at
FROM outbox_events WHERE status = 'dead' ORDER BY id;
```

## 2. Fix the cause
| `last_error` | Cause | Fix |
|---|---|---|
| `no_alert_contacts` | The clinic has no urgent alert email configured | Add `alert_contacts` for the clinic |
| `message_not_found` | The message row is missing (should not happen) | Investigate before re-queueing |
| `RuntimeError`, `HTTPStatusError`, `ConnectError`, … | Email provider problem | Check the Resend status and the API key |
| `lease_expired_at_max_attempts` | The worker crashed mid-handler every time | Check ops-worker logs and memory, then re-queue |

**For an urgent message, phone the clinic now.** Don't wait for the fix.

## 3. Decide: re-queue or abandon
```sql
-- re-queue (after the fix)
UPDATE outbox_events SET status = 'pending', attempts = 0, available_at = now() WHERE id = <id>;
-- or abandon (e.g. the clinic was phoned instead); record why in the incident log
UPDATE outbox_events SET status = 'abandoned' WHERE id = <id>;
COMMIT;
```
`/health/outbox` goes green once no event is `dead`, overdue or failing repeatedly.
