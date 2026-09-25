# ADR 0008 — Hosting region and cross-border disclosure

**Status:** Accepted (owner, 25 Sep 2026): host in Railway's Singapore region for now

## Context
Clinics are in Australia. The voice provider processes call audio and transcripts in the United
States, so full Australian residency is not achievable with the current provider regardless of
where the database runs. Candidate hosting regions: Singapore (current platform) or Sydney.

## Decision
Host in Railway's Singapore region (the nearest available), disclose overseas processing (Australian Privacy Principle 8)
in every clinic agreement and privacy notice, keep a sub-processor register, and store encrypted
backups in an Australian region. Revisit when a clinic contract requires Australian residency.

## Revisit when
A clinic contract requires Australian hosting, or the voice provider offers Australian processing. Moving the database and services to an Australian region together is then a planned migration.
