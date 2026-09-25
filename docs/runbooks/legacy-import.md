# Runbook: importing a clinic's history from the legacy dashboard

ADR 0009 moves each clinic to a new database and copies its history across. `db/import_legacy.py`
does the copy for **one clinic per run**. It runs inside the platform as the one-shot `db-admin`
service, so patient data goes database to database and never through anyone's laptop.

The run is safe to repeat:
- **Dry run by default.** It does everything, verifies it, then rolls back.
- **One transaction.** Nothing is committed unless every imported row matches its source.
- **Never overwrites calls this system already holds.** It skips calls that arrived by webhook, and calls staff have changed here.
- **Output is counts only.**

## Before you start

- The clinic exists in the new database, with its timezone set correctly (`clinics.timezone`).
- You know the legacy **agent ids** whose calls belong to this clinic. That's every agent the clinic's numbers have used, including retired ones. Also note any legacy `practice` names used before calls carried an agent id.
- The legacy database's **public connection URL**. It's in the legacy Railway project: Postgres service → Variables → `DATABASE_PUBLIC_URL`. The owner copies it; nobody else types or sees it. TLS is required automatically for any non-local host.
- The current production call volume is low. The import takes seconds and holds only row locks on the clinic's own rows.

## Run it

Set these on the `db-admin` service in the **target** environment:

| Variable | Value |
|---|---|
| `WASSUP_ROLE` | `import-legacy` |
| `WASSUP_LEGACY_DATABASE_URL` | the legacy public URL (owner pastes it) |
| `WASSUP_IMPORT_CLINIC` | the clinic slug, e.g. `example-clinic` |
| `WASSUP_IMPORT_AGENTS` | comma-separated legacy agent ids |
| `WASSUP_IMPORT_PRACTICE_NAMES` | optional, `\|`-separated legacy practice names |
| `WASSUP_IMPORT_ORPHANS` | `true` only when **every** legacy message and promise belongs to this clinic (see below) |
| `WASSUP_IMPORT_APPLY` | leave unset for the first run |

1. **Dry run.** Deploy `db-admin`: `scripts/deploy-railway.sh <env> db-admin`. Then read its log. It ends `dry run: verified, then rolled back`. Check the counts:
   - `source.calls` against the legacy dashboard's call count for this clinic.
   - `calls.inserted` + `calls.kept_live_or_changed_here` + `calls.refreshed` = `source.calls` + `calls.placeholder_for_orphans`.
   - `messages.not_this_clinic_or_orphan_skipped` is the number of messages left in legacy. They belong to other clinics, or to calls whose webhook was lost (see orphans).
   - `left_behind.*` counts are values this system deliberately doesn't store: caller names, dates of birth, free-text actor names, promise subjects.
2. **Apply.** Set `WASSUP_IMPORT_APPLY=true`, deploy `db-admin` again, and check the log ends `committed`. An `audit_log` row (`legacy.import`) records the counts.
3. **Clean up.** Delete `WASSUP_LEGACY_DATABASE_URL` and `WASSUP_IMPORT_APPLY` from `db-admin`, and set `WASSUP_ROLE=report`.

If a run prints `import refused: verification failed`, nothing was committed. The `verify failed, …` lines name the table and field that differed, but never the values. Fix the cause and run again.

## Orphaned messages

Before September 2026 a lost webhook left messages captured during a call (`w1_messages`) with no call row. Those are exactly the calls that most needed a callback.

With `WASSUP_IMPORT_ORPHANS=true` they're imported under a **placeholder call**. It has no summary, its status is *To do*, and its time is the first message's time, so it shows up in the inbox. Legacy messages carry no clinic of their own. So use this only for a clinic whose agents were the only ones with message tools (in legacy, only one clinic's agent used them).

## During the cutover window

Legacy staff can keep working until you switch them over. Run the import again just before the switch:
- It adds calls and history that arrived since the last run.
- It refreshes the workflow status of imported calls nobody has touched here yet.

After the switch, don't run it again for that clinic.

## Not imported (by design)

- Caller and patient names, dates of birth and legacy patient ids. This system doesn't store them on calls.
- Triage routes, and free-text staff names on history entries (they were typed in, not signed in).
- Promise subjects.

The dry run reports how many of each exist.

Priority flags (`is_priority`, `is_reception_action`) are computed later for every call, imported or live, when the call classifier is ported.
