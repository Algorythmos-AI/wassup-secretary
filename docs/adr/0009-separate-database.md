# ADR 0009 — A new database; legacy history imported by a private ETL

**Status:** Accepted (owner, 25 Sep 2026)

## Context
The original plan had the new services share the legacy database (stamp its schema as a baseline,
add tenant columns, keep legacy writers working through compatibility triggers and a time-boxed
RLS-bypass role). Reviews flagged that path as the riskiest part of the migration, and defining the
legacy schema here would copy content from a private repository into this public one.

## Decision
- WASSUP Secretary owns a fresh database defined only by this repository's migrations.
- Each clinic cuts over one at a time: its voice agent is repointed to `voice-gateway`, so new calls
  land in the new database; rollback is repointing the agent back.
- Historical data is imported once per clinic by an ETL kept in the private legacy repository,
  mapping into this schema and verified by row counts and checksums before the clinic's dashboard
  switches over.

## Consequences
No shared-schema coupling, no compatibility triggers, no bypass role. Until a clinic's history import
is verified, its dashboard keeps reading the legacy system.
