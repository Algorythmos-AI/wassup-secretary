# ADR 0008 — Hosting region and cross-border disclosure

**Status:** Proposed — needs owner decision

## Context
Clinics are in Australia. The voice provider processes call audio and transcripts in the United
States, so full Australian residency is not achievable with the current provider regardless of
where the database runs. Candidate hosting regions: Singapore (current platform) or Sydney.

## Proposal
Host in the nearest available region, disclose overseas processing (Australian Privacy Principle 8)
in every clinic agreement and privacy notice, keep a sub-processor register, and store encrypted
backups in an Australian region. Revisit when a clinic contract requires Australian residency.
