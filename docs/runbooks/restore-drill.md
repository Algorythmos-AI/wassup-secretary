# Runbook: backups and the restore drill

Every night ops-worker copies the whole database out, encrypts it, puts it in the backup store,
fetches it back and checks it, then prunes old copies. `db/restore.py` loads such an archive
into an empty database at the same schema revision and proves, table by table, that what came
back is what was dumped. The drill below is run **once before go-live and every quarter**, and
recorded in `docs/readiness/<date>.md` (item 7).

## What a backup is

- `wassup-<environment>-<UTC time>-r<revision>.wsb`: a gzip'd tar of `manifest.json` and one
  `tables/<name>.tsv` per table (Postgres COPY text, rows ordered by primary key), encrypted as a
  stream of AES-256-GCM frames under a key derived from the master key with a random salt. Any
  truncation, alteration, reordering or wrong key is refused as a whole.
- The manifest records the schema revision, every table's columns, row count and SHA-256, and
  every sequence's position. Row counts and hashes are what a restore is checked against.
- It is taken in one REPEATABLE READ snapshot as `wassup_backup`: the one database role that
  bypasses row-level security, and one that can only SELECT (`tests/tenancy` pins both).
- An upload is written as `<name>.pending`, fetched back, decrypted and checked against the
  manifest, and only then renamed to its real name. Anything under a real name was verified in
  the store; a failed attempt removes its pending upload. Verified is not restored: that is the
  drill.
- Every dump and every restore check pins the session's output settings (UTC, ISO dates) and
  orders rows by the text of the primary key under the C collation, so an archive restores and
  checks identically on a server with another time zone or locale.

## Where it runs

- **Production: a cron service.** A service `backup` (the ops-worker image) with
  `WASSUP_ROLE=backup`, the backup variables below and a cron schedule of `30 16 * * *` (UTC,
  so 02:30 or 03:30 in Sydney). It backs up, pings `WASSUP_BACKUP_HEARTBEAT_URL`, and exits.
  The all-clinics database credential and the key then live only in that service. ops-worker
  gets **only the store variables** and watches: `/health/backup` is failing when no archive
  under 36 hours old is in the store, and ops is emailed once a day while that lasts.
- **Staging or small setups: in ops-worker.** Give ops-worker the database URL and key as well,
  and it backs up once a day after `WASSUP_BACKUP_LOCAL_TIME` itself.
- After a failure the next attempt is an hour later, not every minute.

## Configuration

| Variable | Meaning |
|---|---|
| `WASSUP_BACKUP_DATABASE_URL` | `postgresql://wassup_backup:${{db-admin.WASSUP_PASSWORD_BACKUP}}@…` (the backup role's own URL; `db-admin` gets `WASSUP_PASSWORD_BACKUP=${{secret(40)}}` and the bootstrap is re-run once) |
| `WASSUP_BACKUP_KEY_HEX` | 64 hex characters (32 bytes). Generate in the platform: `${{secret(64, "0123456789abcdef")}}`. **Copy it to the password manager**: without it every archive is noise, and it must never be in git, a chat or a log. |
| `WASSUP_BACKUP_S3_BUCKET`, `WASSUP_BACKUP_S3_ACCESS_KEY_ID`, `WASSUP_BACKUP_S3_SECRET_ACCESS_KEY` | an S3-compatible bucket in `ap-southeast-2` (or Cloudflare R2 with `WASSUP_BACKUP_S3_ENDPOINT_URL`); `WASSUP_BACKUP_S3_PREFIX` (default `wassup-backups`), `WASSUP_BACKUP_S3_REGION`. Turn on versioning and object lock in the bucket: a deleted or overwritten archive then still exists. The key pair must be able to put, get, list and delete under the prefix and nothing else. |
| `WASSUP_BACKUP_DIR` | a directory instead of a bucket (a mounted volume). Fine for staging; production uses a bucket in another account. |
| `WASSUP_BACKUP_LOCAL_TIME`, `WASSUP_BACKUP_TIMEZONE` | when to run each day (default 03:30 Australia/Sydney) |
| `WASSUP_BACKUP_KEEP` | archives kept per environment (default 30) |
| `WASSUP_BACKUP_HEARTBEAT_URL` | pinged after a **verified** backup only, so a missed night pages |
| `WASSUP_BACKUP_ON_START` | `true` to also back up when the worker starts (useful for a drill) |

A setting that asks for backups but can't work (a bucket without its keys, a malformed key, a
database URL without a key) never stops ops-worker: `/health/backup` reports `failing` with the
reason (`store_misconfigured`, `key_invalid`, `needs_both_database_url_and_key`, `no_store`).

`GET /health/backup` on ops-worker: `ok` (newest archive under 36 h old), `pending` (the
worker started less than 36 h ago and hasn't run one yet), `failing` (503: the last run failed
or none for 36 h), `unconfigured` (503). Monitor it. A failure also emails ops once a day.

A backup by hand: deploy `db-admin` (it reuses the ops-worker image) with `WASSUP_ROLE=backup`
and the variables above; it prints the archive name, size, tables, rows and `verified=True`.

## The drill (restore into a temporary environment)

Never restore into a live environment except in an actual disaster (below). The drill proves
the archive and the procedure on a throwaway database.

1. **Temporary environment.** In Railway, create environment `drill` from staging (or an empty
   Postgres service anywhere). Bootstrap its roles and migrate it to **exactly** the revision in
   the archive name (`r0013` → `alembic upgrade 0013`; a newer schema is refused).
   The fast way: `railway environment new drill --duplicate staging`, then deploy `db-admin`
   (`WASSUP_ROLE=bootstrap`) and `ops-worker` (its pre-deploy step migrates to head) from the
   same commit the archive was taken with.
2. **Point db-admin at the drill database** and set: `WASSUP_ROLE=restore`,
   `WASSUP_BACKUP_KEY_HEX`, the store variables (or upload the archive and set
   `WASSUP_RESTORE_PATH`), `WASSUP_RESTORE_SOURCE=<environment the archive is from>`,
   `WASSUP_RESTORE_NAME=latest` (the newest archive **from that environment**, by its time) or
   a name, and `WASSUP_ENVIRONMENT=staging`. An archive from another environment is refused.
3. **Dry run** (no `WASSUP_RESTORE_APPLY`): deploy `db-admin`. The log ends
   `dry run: loaded and verified, then rolled back` with per-table counts. Anything else is a
   finding: fix it before relying on the backups.
4. **Apply:** `WASSUP_RESTORE_APPLY=true`, deploy again; the log ends `committed`. If the drill
   database already has rows (a second attempt), add `WASSUP_RESTORE_TRUNCATE=true`.
5. **Boot the app on it:** deploy core-api and web in the drill environment pointed at that
   database; sign in; the inbox shows the clinics' calls as of the backup time. Compare a few
   counts with the source (`WASSUP_ROLE=report` on both).
6. **Record** the archive name, its `created_at`, the counts and the time the whole drill took in
   `docs/readiness/<date>.md`. Then **delete the drill environment** (it holds real data).

Targets: recovery point 24 h for staff actions (call data itself is replayable from the voice
provider); recovery time 4 h from decision to a booted app.

## Restoring for real (disaster)

Same tool, into the production database, after these decisions are written down: which archive,
why the current data is unrecoverable, who approved. Then `WASSUP_ENVIRONMENT=production`
requires `WASSUP_PRODUCTION_ACK=<database name>` on the apply (and on any run with truncate,
even a dry run, because truncating locks every table while it runs), `WASSUP_RESTORE_TRUNCATE=true`
if the database is not empty, and voice-gateway must be **stopped first** so nothing is written
during the load (a restore is one transaction; anything written after the archive was taken is
gone — replay from the voice provider covers calls, not staff actions).

## Invariants (tested)

- The backup role bypasses RLS and can only SELECT; no other role bypasses RLS.
- A dump as any other role is refused (a backup missing rows would be worse than none).
- Every table except `alembic_version` is in the archive; both clinics' rows are present.
- Tampering of any kind (wrong key, flipped byte, truncation, reordering, appended data) is
  refused whole; a store that returns different bytes fails the backup.
- A restore refuses a different schema revision, a non-empty target, or an archive from another
  environment than named, and rolls back if any table's rows or bytes differ from the manifest
  after loading. It restores identically into a server with another time zone.
- An unverified upload never appears under an archive name, so it is never taken for a backup,
  restored as `latest`, or counted when pruning.
- Database errors are reported by class only (their messages can quote rows).
