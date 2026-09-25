# Runbook: a clinic's patient list

`lookup_patient` lets the voice agent confirm that a caller is a known patient: exact first name,
last name and date of birth, answered with an opaque reference and never a name. It can only
confirm against the clinic's own list in `patients`, loaded with `db/import_patients.py`.

## What the lookup does, and does not, reveal

- `{"matched": false}` for no match, more than one match, a deceased patient, a caller who has
  used up their lookups, or a withheld caller ID. These are indistinguishable on purpose.
- `{"matched": true, "patient_ref": …, "verified": true|false}` for exactly one living match.
  `verified` is true only when the caller's number and the number on the patient's record are
  the same number once both are written the one canonical way (`+61…`; a foreign number never
  equals an Australian one, whatever its last digits). **The agent may say patient-specific
  things only when `verified` is true**; otherwise it treats the caller as unverified and takes
  a message.
- **What `verified` is not:** proof of identity. Caller ID can be spoofed over VoIP and is not
  authenticated in Australia. It is enough for non-clinical, patient-specific handling (which
  clinic, which doctor, an existing message); anything clinical still goes to a person who
  confirms identity themselves.
- Limits: 2 lookups per call, 5 per caller number per 24 hours, none with caller ID withheld
  (empty, a provider marker such as "anonymous", or Twilio's blocked-ID number `+266696687`).
  The per-caller count is kept as a keyed hash of the number, never the number itself.

## Loading the list

The list comes from the clinic's practice-management system as a CSV with a header row:
`source_pms_id, first_name, last_name, date_of_birth, phone, is_deceased` (extra columns are
ignored; dates `YYYY-MM-DD` or `DD/MM/YYYY`; `is_deceased` true/false, yes/no, 1/0 or blank).

It is loaded inside the platform by the one-shot `db-admin` service, from either:
- **a URL** the clinic or owner has uploaded the file to (a short-lived signed link, https only),
  with the file's SHA-256, so a truncated or substituted file is refused; or
- **a path**, when running `railway run` from the machine holding the export.

Never commit or email the file, and delete uploads once the import is committed.

1. Set on `db-admin` in the target environment: `WASSUP_ROLE=import-patients`,
   `WASSUP_PATIENTS_CLINIC=<slug>`, and `WASSUP_PATIENTS_URL` + `WASSUP_PATIENTS_SHA256`
   (or `WASSUP_PATIENTS_PATH`). Leave `WASSUP_PATIENTS_APPLY` unset.
2. Deploy `db-admin`: the log ends `dry run: verified, then rolled back` with counts
   (`source_rows`, `refused`, `inserted`, `refreshed`, `unchanged`, `total_after`) and the source
   ids of refused rows (no names, dates or numbers are ever printed). A refused row lacks a name,
   date of birth or usable id, or repeats an id: fix it in the clinic's system and export again.
3. Apply: `WASSUP_PATIENTS_APPLY=true` (and in production `WASSUP_PRODUCTION_ACK=<slug>`),
   deploy again; the log ends `committed`. An `audit_log` row (`patients.import`) records the
   counts.
4. Clean up: remove the URL, checksum, apply and acknowledgement variables; `WASSUP_ROLE=report`.

Rows are upserted by `source_pms_id`: existing patients are refreshed, new ones added, and
**nobody is deleted** by an import. A patient who should no longer be matched is marked deceased
or removed in the clinic's system and re-exported; removal from this list is a deliberate,
separate operator action.
