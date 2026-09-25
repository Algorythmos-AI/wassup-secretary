# Runbook: replay exhausted

**Signal:**
- an email "WASSUP replay exhausted", or
- `GET /health/replay` on ops-worker returns 503.

**What it means:** voice-gateway stored a webhook event or a write-tool request, but couldn't
finish processing it. ops-worker then replayed it 5 times (at about 1, 2, 4, 8 and 16 minutes) and
every attempt failed. The likely consequences:
- a call is missing from the dashboard, or
- an urgent message never reached the clinic.

`events_stuck` or `tools_stuck` without anything exhausted means the replay job itself isn't
running. Check that `WASSUP_VOICE_GATEWAY_URL` is set and that ops-worker can reach voice-gateway.

## 1. Look at the error codes (no personal data)
```sql
SELECT id, event, provider_call_id, replay_attempts, error, received_at
FROM retell_events_raw WHERE processed_at IS NULL AND replay_attempts >= 5
  AND (error IS NULL OR error NOT LIKE 'quarantined:%');
SELECT id, tool, clinic_slug, provider_call_id, replay_attempts, last_error, received_at
FROM tool_requests_raw WHERE completed_at IS NULL AND replay_attempts >= 5;
```
| Error | Meaning |
|---|---|
| `replay_unreachable:*` | ops-worker can't reach voice-gateway. Check the private network and `WASSUP_VOICE_GATEWAY_URL`. |
| `replay_http_401` | Signature rejected. ops-worker and voice-gateway disagree on the Retell key (for example mid-rotation). |
| `replay_http_503` | The database is still unavailable to voice-gateway. |
| `replay_degraded` | The tool itself still fails (a bug, or configuration such as an unknown agent). |
| `processing_failed:<Type>` | A voice-gateway bug. Check its logs for that exception type. |

## 2. For a tool request: act on the content first
`capture_message` with `"urgent": true` in `payload.args` means **phone the clinic now**. Read the
payload only for this purpose, and log that you did.

## 3. Fix, then retry or close
```sql
-- retry (after the fix): the next replay pass picks it up
UPDATE tool_requests_raw SET replay_attempts = 0 WHERE id = <id>;
UPDATE retell_events_raw SET replay_attempts = 0 WHERE id = <id>;
-- or close it (handled by hand); record why in the incident log
UPDATE tool_requests_raw SET completed_at = now(), outcome = 'abandoned' WHERE id = <id>;
UPDATE retell_events_raw SET processed_at = now(), error = 'abandoned' WHERE id = <id>;
```
