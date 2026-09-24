# Runbook: phone line down / calls not reaching the dashboard

**Triggered by:**
- a `WASSUP line check FAILED` email;
- `/health/canary` or `/health/freshness` on ops-worker returning 503;
- a clinic reporting missing calls.

```
Patient → clinic phone system divert → telephony number → SIP trunk → voice provider agent
        → voice-gateway webhook → database → dashboard
```

1. **Did calls reach the voice provider?** Check its call log for the line's agent.

   | What you see | Meaning | Go to |
   |---|---|---|
   | Calls there but missing here | ingestion is broken (`/health/freshness` shows `gap`) | step 4 |
   | No calls at all, not even failed ones | telephony is broken upstream | step 2 |
2. **Telephony account.**
   - Suspended or unpaid? Pay, and enable auto-recharge.
   - Numbers still owned?
   - SIP trunk origination still points at the voice provider?
3. **Voice provider.**
   - Is the number bound to the expected published agent version?
   - Is the agent's webhook URL correct?
   - Account billing and concurrency.
4. **voice-gateway.**
   - Check `/health` and the recent deploys.
   - Look in the logs for `webhook_rejected` (bad signature, e.g. a key rotation without the previous key set) or `webhook_db_unavailable`.
   - Raw events are stored first, so replay from `retell_events_raw` where `processed_at IS NULL`.
5. **Clinic phone system.** The line check rings the AI numbers directly, so it doesn't test the clinic's divert. If the line check is green but the clinic still says calls ring out, it's the clinic's call-forward rule (after-hours, no-answer, public holidays).

**After recovery:** ask the clinic to call back everyone on its phone system's missed-call log for the outage window, and record the incident.
