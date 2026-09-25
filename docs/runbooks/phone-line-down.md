# Runbook: phone line down / calls not reaching the dashboard

**Triggered by:**
- a `WASSUP line check FAILED` or `WASSUP telephony account needs attention` email;
- `/health/canary`, `/health/freshness` or `/health/telephony` on ops-worker returning 503;
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
2. **Telephony account.** `/health/telephony` shows what ops-worker last saw. It checks every
   5 minutes: `account` (active/suspended/closed), `balance`, and `missing_numbers`.
   - `account_suspended` or `balance_low`: pay, then enable auto-recharge with a backup card.
     This was the cause of the September 2026 outage.
   - `numbers_missing`: a line number is no longer on the account. Treat this as urgent, because
     the clinic's phone system may be diverting patients to a number someone else now owns.
   - `credentials_rejected`: the monitor's API key was revoked or the account is unusable. Log in
     to the console to see which.
   - SIP trunk origination still points at the voice provider?
3. **Voice provider.**
   - Is the number bound to the expected published agent version?
   - Is the agent's webhook URL correct?
   - Account billing and concurrency.
4. **voice-gateway.**
   - Check `/health` and the recent deploys.
   - Look in the logs for `webhook_rejected` (bad signature, e.g. a key rotation without the previous key set) or `webhook_db_unavailable`.
   - Raw events and write-tool requests are stored first. ops-worker replays them automatically;
     `/health/replay` shows anything it could not recover (see [replay-exhausted](replay-exhausted.md)).
5. **Clinic phone system.** The line check rings the AI numbers directly, so it doesn't test the clinic's divert. If the line check is green but the clinic still says calls ring out, it's the clinic's call-forward rule (after-hours, no-answer, public holidays).

**After recovery:** ask the clinic to call back everyone on its phone system's missed-call log for the outage window, and record the incident.
