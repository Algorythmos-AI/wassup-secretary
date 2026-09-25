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
- Crossing the boundary is only possible through reviewed SECURITY DEFINER resolvers, each granted
  to one role, owned by a NOLOGIN `wassup_resolver` role that can read only routing tables. Every
  definer function pins `search_path = pg_catalog, public, pg_temp` (pg_temp is otherwise searched
  *first* for tables, so a temporary view could impersonate `clinic_memberships`), and no login role
  holds the TEMPORARY privilege. CI enforces both.
- The audit log is append-only for app roles and hash-chained per clinic by a trigger owned by a
  separate NOLOGIN `wassup_auditor` role (migration 0004).
- `tests/tenancy` checks isolation behaviourally and the catalog structurally in CI.

## Threat model (what RLS does and does not defend)
- **Defends:** application bugs — a forgotten `WHERE clinic_id`, a wrong join, a new endpoint that
  reads a table directly. With no declared clinic the query sees nothing; with one clinic declared
  it sees only that clinic.
- **Does not defend:** arbitrary SQL executed *as* an app role (e.g. SQL injection). Such an attacker
  can declare any clinic id it knows. The defences there are: fixed SQL statements with bound
  parameters only (no SQL assembled at runtime, enforced by ruff S608), random UUID clinic ids, and
  no app role able to list other clinics' ids.

## Consequences
- Tenant queries must run inside a transaction that set the context (a repository helper does it).
- Connection poolers must use transaction mode and `SET LOCAL` only.
- Platform jobs (ops-worker) still go through RLS by declaring the active clinic list.
