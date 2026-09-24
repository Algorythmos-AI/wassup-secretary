# ADR 0003 — Clinic isolation enforced by Postgres row-level security

**Status:** Accepted

## Context
The product will hold many clinics' health information. Application-level `WHERE clinic_id = …`
alone fails open on the first forgotten filter. The legacy system had no tenant column at all.

## Decision
- Every tenant table has `clinic_id` with `ENABLE` and `FORCE ROW LEVEL SECURITY` and one policy:
  `clinic_id = ANY(declared clinics)`. A transaction declares its clinics with
  `set_config('app.clinic_ids', '{…}', true)`; no declaration means zero rows.
- No policy has a bypass clause and no role is SUPERUSER or BYPASSRLS — including the table owner.
- One login role per service with least-privilege grants and its own `statement_timeout`.
- Crossing the boundary is only possible through three reviewed SECURITY DEFINER resolvers, each
  granted to one role, owned by a NOLOGIN `wassup_resolver` role that can read only routing tables.
- `tests/tenancy` checks isolation behaviourally and the catalog structurally in CI.

## Consequences
- Tenant queries must run inside a transaction that set the context (a repository helper does it).
- Connection poolers must use transaction mode and `SET LOCAL` only.
- Platform jobs (ops-worker) still go through RLS by declaring the active clinic list.
