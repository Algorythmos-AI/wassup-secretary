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

## 2. Recover the calls

Decisions are runs of the one-shot `db-admin` service with `WASSUP_ROLE=ops` (a dry run until
`WASSUP_OPS_APPLY=true`; in production also `WASSUP_PRODUCTION_ACK=<action>`; see
[outbox-dead-letter](outbox-dead-letter.md) for how). `WASSUP_OPS_ACTION=list` shows each
unresolved record's `id`, `reason`, `agent_id` and time, never its payload.

- **Webhook events:** `WASSUP_OPS_ACTION=requeue-quarantined-webhooks`,
  `WASSUP_OPS_AGENT=<agent>` makes that agent's quarantined events eligible for the replay job,
  which re-submits them within minutes (after step 1, so they now resolve).
- **Tool requests** cannot be recovered after the call ends. If a quarantined `capture_message`
  was urgent, **phone the clinic**.

## 3. Record the decision

`WASSUP_OPS_ACTION=resolve-quarantine`, `WASSUP_OPS_IDS=<id>[,<id>…]`,
`WASSUP_OPS_RESOLUTION=replayed` (or `not_ours`, `handled_by_phone`).

`/health/quarantine` goes green once nothing is unresolved. Resolved records are deleted by
retention after 90 days; unresolved ones never are.
