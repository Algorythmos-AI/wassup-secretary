# ADR 0010: Recover unfinished work by replaying it through voice-gateway's own endpoint

**Status:** Accepted

## Context
voice-gateway stores every signed webhook event and write-tool request before processing it (ADR 0004). Processing can still fail after that:
- the database stumbles;
- a tool runs past its 1.5 s budget;
- a deploy lands a bug.

Before this decision:
- a failed tool call answered the caller with `ok: true`, so an urgent message could be lost while the caller was told it had been passed on;
- a failed webhook was acknowledged and never retried.

## Decision
- **Truthful fallbacks.** A write tool that did not commit answers `ok: false`, and the agent must say so. The contract is in `docs/voice-tools.md`.
- **Replay through the front door.** ops-worker re-submits each unfinished raw item to voice-gateway's real endpoints. It uses the same JSON and a fresh Retell-style signature, so recovery runs the **same code path** as live traffic. Idempotency makes a replay of something that did complete a no-op:
  - calls use upsert precedence;
  - tools use their dedupe key.
- **Bounded.** Replays back off (1, 2, 4, 8 and 16 minutes). After 5 attempts, ops is emailed and `/health/replay` stays red until an operator decides.
- **Minimal retention of what callers said.** Lookups are never stored raw. Finished raw rows are deleted after 90 days.

## Alternatives rejected
- **Share the handler code with ops-worker.** That puts two copies of the voice path into production and couples ops-worker's deploys to voice-gateway's code.
- **Rebuild from the provider's transcript.** It is lossy (tool arguments aren't always recoverable) and depends on the provider's retention.
- **Rely on the provider's own retries.** Their semantics are unverified, and they can't recover from a bug fixed hours later.

## Consequences
- ops-worker needs voice-gateway's private URL (`WASSUP_VOICE_GATEWAY_URL`) and the Retell signing key.
- A caller whose request degraded may be contacted twice, once because they were told to call the clinic and once from the recovered message. That is safe, and preferable to silence.
