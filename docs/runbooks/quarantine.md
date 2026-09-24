# Runbook: quarantined calls

**Signal:**
- an email "WASSUP quarantined calls need a decision", or
- `GET /health/quarantine` on ops-worker returns 503.

**What it means:** voice-gateway received signed provider traffic whose agent and dialled number
did not resolve to exactly one clinic, so it was set aside. A quarantined **webhook** never becomes
a call record, so that call is on no dashboard until someone acts. A quarantined **tool request**
was answered with the tool's fallback: the caller was told their message was *not* saved.

| `by_reason` | Usual cause |
|---|---|
| `unknown_agent_or_number` | A new or re-created agent isn't in `clinic_voice_agents`, a number isn't in `clinic_phone_numbers`, a row was deactivated, or the clinic is suspended. |
| `tool_unknown_agent_or_number` | The same, seen on a tool call. |
| `tool_clinic_slug_mismatch` | The agent's tool URL has the wrong clinic slug. Fix the agent's tool URLs, then publish and rebind. |

## 1. Fix the mapping first
Correct `clinic_voice_agents` or `clinic_phone_numbers`, or the agent's configuration, so new calls stop being quarantined. `/health/voice-config` shows which numbers disagree.

## 2. Recover the calls (as the database admin, inside a transaction)
```sql
-- Webhook events: make them eligible for the replay job (it re-submits them within minutes).
UPDATE retell_events_raw SET error = 'processing_failed:requeued', replay_attempts = 0
WHERE error LIKE 'quarantined:%' AND agent_id = '<agent>';
```
Tool requests cannot be recovered after the call ends. If a quarantined `capture_message` was urgent, **phone the clinic**. The payload is in `quarantine_events`; read it only for this purpose.

## 3. Record the decision
```sql
UPDATE quarantine_events SET resolved_at = now(), resolution = 'replayed'   -- or 'not_ours', 'handled_by_phone'
WHERE resolved_at IS NULL AND agent_id = '<agent>';
```
`/health/quarantine` goes green once nothing is unresolved. Resolved records are deleted by retention after 90 days; unresolved ones never are.
