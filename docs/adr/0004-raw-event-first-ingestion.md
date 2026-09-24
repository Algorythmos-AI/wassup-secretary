# ADR 0004 — Store raw provider events before processing; idempotent tools

**Status:** Accepted

## Context
Voice providers retry webhooks and may re-invoke tools. A processing bug must not lose a call, and
a retry must not page a doctor twice. Tool calls happen mid-conversation, so latency is audible.

## Decision
- The webhook verifies the signature on the raw bytes, then inserts the payload into
  `retell_events_raw` (unique per call and event) **before** any processing. Processing failures are
  flagged for replay and still acknowledged; a database outage returns 5xx so the provider retries.
- Every tool invocation is recorded in `tool_invocations` with a unique dedupe key (provider tool-call
  id when present, else a hash of call, tool and arguments). A repeat returns the stored result.
- Tools have a hard deadline (1.5 s budget, 300 ms pool acquire, 1 s statement timeout) and a safe
  fallback answer, so the caller never hears dead air.
- The clinic is resolved from the signed agent id **and** the dialled number; a mismatch is quarantined.

## Consequences
Nothing is lost on a bad deploy (replay from the raw log), duplicates are structurally impossible,
and the tool-latency SLO is measured from `tool_invocations`.
