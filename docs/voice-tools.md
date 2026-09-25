# Voice tool contract

The voice agent calls tools at `POST /v1/retell/tools/{clinic_slug}/{tool}` on voice-gateway.
This page is the contract between the tool responses and what the agent says. Agent prompts must
follow it.

## Principle: the agent only claims what actually happened

The response is the truth about what was saved. The agent must never tell a caller that a message
was passed on, or a callback arranged, unless the tool said so.

| Response | Meaning | What the agent should say |
|---|---|---|
| `{"ok": true}` | Saved. For `urgent: true`, the clinic's alert contacts are being notified. | Confirm, e.g. "I've passed that on to the team." |
| `{"ok": false, "degraded": true}` | **Not saved** right now: time budget exceeded, database trouble, or a configuration problem. The request is kept and retried in the background, but nobody can promise when. | Be honest and give a safe path: "I'm sorry, I couldn't pass that on just now. If it's urgent, please call the clinic directly on …, or 000 in an emergency." |
| `{"ok": false, "error": "invalid_arguments"}` | The agent sent something malformed. | Re-ask for the missing detail. |
| `{"ok": true, "synthetic": true}` | A line-check call. Nothing is stored. | (Line-check agent only.) |
| `{"matched": true, "patient_ref": "…"}` | Exactly one living patient matched. `patient_ref` is opaque. It is never a name. | Continue as a known patient. Don't read back personal details. |
| `{"matched": false}` (optionally `"reason"` or `"degraded"`) | No match, or several matches, or the patient is deceased, or a lookup limit was reached (2 per call, 5 per caller number per day), or the caller ID is withheld, or the database was unavailable. All look the same **on purpose**. | Continue as a new or unverified caller. |
| `{"matched": true, "patient_ref": "…", "verified": true}` | Exactly one living patient matches **and** the caller's number is the number on that patient's record (compared in one canonical `+61…` form; caller ID is not proof of identity). | The agent may refer to patient-specific, non-clinical facts. |
| `{"matched": true, "patient_ref": "…", "verified": false}` | Exactly one living patient matches, but the caller's number is not the one on file. | Treat as unverified: take a message; say nothing patient-specific. |

## Guarantees behind the responses

- **Exactly once.** A retried or replayed request returns the first answer and never writes twice.
  The dedupe key is derived from the call, the tool and the arguments.
- **Nothing said is lost.** Write tools (`capture_message`, `create_promise`) commit the raw request
  before running. If the tool then fails, ops-worker replays it within minutes. An urgent message
  recovered this way still alerts the clinic. After 5 failed replays, ops is emailed and
  `/health/replay` goes red. See [replay-exhausted](runbooks/replay-exhausted.md).
- **Lookups are not kept raw.** A name and date of birth are not stored for later. A lookup after the
  call has ended would help nobody.
- **Hard time budget:** `WASSUP_TOOL_BUDGET_MS` (1.5 s by default). Past the budget the fallback
  above is returned. Keep the tool's `timeout_ms` in the Retell agent comfortably above it
  (about 3 s) and set `max_retry` to 0.
