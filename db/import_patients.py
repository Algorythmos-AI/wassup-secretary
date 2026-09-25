"""Load or refresh one clinic's patient list (run inside the platform as ``db-admin``).

The voice agent's ``lookup_patient`` can only confirm a caller against this list. It comes from
the clinic's practice-management system as a CSV export. The file reaches the platform either
as a path (mounted, or ``railway run`` from the exporting machine) or as an ``https`` URL the
clinic or owner has uploaded it to (a short-lived signed link), always with its SHA-256 so a
truncated or substituted file is refused. Nothing is ever written from a laptop into git.

    WASSUP_ADMIN_DATABASE_URL     this system's database (admin URL)
    WASSUP_PATIENTS_CLINIC        clinic slug (must exist)
    WASSUP_PATIENTS_PATH          the CSV file, or
    WASSUP_PATIENTS_URL           an https URL to it (then WASSUP_PATIENTS_SHA256 is required)
    WASSUP_PATIENTS_SHA256        hex digest the file must have (required for a URL)
    WASSUP_PATIENTS_APPLY         "true" to commit; otherwise a dry run: everything is done and
                                  verified, then rolled back
    WASSUP_PRODUCTION_ACK         in production, an apply also needs this set to the clinic slug

CSV columns (header row, any order; extra columns are ignored):
    source_pms_id, first_name, last_name, date_of_birth (YYYY-MM-DD or DD/MM/YYYY), phone,
    is_deceased (true/false, yes/no, 1/0; blank = false)

Rows are upserted by (clinic, source_pms_id): a row already present is refreshed, a new one
added; nothing is ever deleted (a patient leaving the export is a clinic decision, made in the
clinic's system). Names, dates and numbers are never printed; the output is counts and the
source_pms_ids of rows that were refused, which are the clinic's own identifiers.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import uuid
from datetime import date, datetime
from typing import Any
from urllib.request import urlopen

import psycopg
from psycopg.rows import dict_row

COLUMNS = ("source_pms_id", "first_name", "last_name", "date_of_birth", "phone", "is_deceased")
TRUE = {"true", "yes", "y", "1", "t", "deceased"}
FALSE = {"", "false", "no", "n", "0", "f"}
MAX_ROWS = 200_000


class ImportRefused(Exception):
    pass


def _url(raw: str) -> str:
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix) :]
    return raw


def read_file(path: str | None, url: str | None, sha256: str | None) -> bytes:
    if path:
        with open(path, "rb") as f:
            data = f.read()
    elif url:
        if not url.startswith("https://"):
            raise ImportRefused("WASSUP_PATIENTS_URL must be https")
        if not sha256:
            raise ImportRefused("WASSUP_PATIENTS_SHA256 is required with a URL")
        with urlopen(url, timeout=60) as response:  # noqa: S310 - https enforced above
            data = response.read(64 * 1024 * 1024)
    else:
        raise ImportRefused("WASSUP_PATIENTS_PATH or WASSUP_PATIENTS_URL is required")
    if sha256 and hashlib.sha256(data).hexdigest().lower() != sha256.lower():
        raise ImportRefused("the file's SHA-256 does not match WASSUP_PATIENTS_SHA256")
    return data


def _dob(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError("date_of_birth")


def normalise_phone(value: str) -> str | None:
    """Australian numbers to E.164; anything else kept as digits with a leading +."""
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return None
    if digits.startswith("61") and len(digits) == 11:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+61" + digits[1:]
    if len(digits) == 9 and digits[0] in "2345678":
        return "+61" + digits
    return "+" + digits if 7 <= len(digits) <= 15 else None


def parse_rows(data: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Valid rows and the source ids of refused ones (refused = unusable for a lookup)."""
    text = data.decode("utf-8-sig", errors="strict")
    reader = csv.DictReader(io.StringIO(text))
    fields = {f.strip().lower() for f in (reader.fieldnames or [])}
    missing = {"source_pms_id", "first_name", "last_name", "date_of_birth"} - fields
    if missing:
        raise ImportRefused(f"CSV is missing columns: {', '.join(sorted(missing))}")
    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    seen: set[str] = set()
    for raw in reader:
        item = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        pms = item.get("source_pms_id", "")
        if not pms or len(pms) > 100 or pms in seen:
            refused.append(pms or "(blank id)")
            continue
        seen.add(pms)
        try:
            dob = _dob(item.get("date_of_birth", ""))
        except ValueError:
            refused.append(pms)
            continue
        first, last = item.get("first_name", "")[:100], item.get("last_name", "")[:100]
        if not (first and last and dob):
            refused.append(pms)  # a lookup needs all three
            continue
        deceased_raw = item.get("is_deceased", "").lower()
        if deceased_raw not in TRUE | FALSE:
            refused.append(pms)
            continue
        rows.append(
            {
                "source_pms_id": pms,
                "first_name": first,
                "last_name": last,
                "date_of_birth": dob,
                "phone": normalise_phone(item.get("phone", "")),
                "is_deceased": deceased_raw in TRUE,
            }
        )
        if len(rows) > MAX_ROWS:
            raise ImportRefused(f"more than {MAX_ROWS} rows; split the file")
    return rows, refused


_UPSERT = """
    INSERT INTO patients (clinic_id, source_pms_id, first_name, last_name, date_of_birth, phone,
                          is_deceased)
    VALUES (%(clinic_id)s, %(source_pms_id)s, %(first_name)s, %(last_name)s, %(date_of_birth)s,
            %(phone)s, %(is_deceased)s)
    ON CONFLICT (clinic_id, source_pms_id) DO UPDATE SET
      first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name,
      date_of_birth = EXCLUDED.date_of_birth, phone = EXCLUDED.phone,
      is_deceased = EXCLUDED.is_deceased, updated_at = now()
    WHERE (patients.first_name, patients.last_name, patients.date_of_birth, patients.phone,
           patients.is_deceased) IS DISTINCT FROM
          (EXCLUDED.first_name, EXCLUDED.last_name, EXCLUDED.date_of_birth, EXCLUDED.phone,
           EXCLUDED.is_deceased)
    RETURNING (xmax = 0) AS inserted
"""
_VERIFY = """
    SELECT source_pms_id, first_name, last_name, date_of_birth, phone, is_deceased
    FROM patients WHERE clinic_id = %(c)s AND source_pms_id = ANY(%(ids)s)
"""


class _DryRun(Exception):
    pass


def run(admin_url: str, slug: str, data: bytes, *, apply: bool) -> dict[str, int]:
    rows, refused = parse_rows(data)
    counts = {"source_rows": len(rows) + len(refused), "refused": len(refused)}
    with psycopg.connect(_url(admin_url), row_factory=dict_row) as conn:
        clinic = conn.execute("SELECT id FROM clinics WHERE slug = %s", (slug,)).fetchone()
        if clinic is None:
            raise ImportRefused(f"no clinic with slug {slug!r}")
        clinic_id = uuid.UUID(str(clinic["id"]))
        try:
            with conn.transaction():
                conn.execute("SET LOCAL ROLE wassup_owner")
                conn.execute("SELECT set_config('app.clinic_ids', %s, true)", (f"{{{clinic_id}}}",))
                conn.execute("SET LOCAL lock_timeout = '5s'")
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('patients-import:' || %s))",
                    (str(clinic_id),),
                )
                inserted = refreshed = unchanged = 0
                for row in rows:
                    got = conn.execute(_UPSERT, {**row, "clinic_id": clinic_id}).fetchone()
                    if got is None:
                        unchanged += 1
                    elif got["inserted"]:
                        inserted += 1
                    else:
                        refreshed += 1
                # Verify: every row reads back exactly as the file says.
                stored = {
                    r["source_pms_id"]: r
                    for r in conn.execute(
                        _VERIFY, {"c": clinic_id, "ids": [r["source_pms_id"] for r in rows]}
                    )
                }
                problems = 0
                for row in rows:
                    got = stored.get(row["source_pms_id"])
                    if got is None or any(
                        got[k] != row[k] for k in COLUMNS if k != "source_pms_id"
                    ):
                        problems += 1
                if problems:
                    raise ImportRefused(f"verification failed for {problems} rows; nothing written")
                total = int(
                    conn.execute(
                        "SELECT count(*) FROM patients WHERE clinic_id = %s", (clinic_id,)
                    ).fetchone()["count"]
                )
                counts.update(
                    inserted=inserted, refreshed=refreshed, unchanged=unchanged, total_after=total
                )
                conn.execute(
                    "INSERT INTO audit_log (clinic_id, actor_service, action, target_type, "
                    "target_id, detail) VALUES (%s, 'db-admin', 'patients.import', 'clinic', %s, "
                    "CAST(%s AS jsonb))",
                    (clinic_id, str(clinic_id), _json(counts, apply)),
                )
                if not apply:
                    raise _DryRun
        except _DryRun:
            pass
    if refused:
        print(
            "refused source ids: " + ", ".join(refused[:50]) + (" …" if len(refused) > 50 else "")
        )
    return counts


def _json(counts: dict[str, int], apply: bool) -> str:
    return json.dumps({**counts, "apply": apply})


def main() -> int:
    env = os.environ
    admin_url, slug = (
        env.get("WASSUP_ADMIN_DATABASE_URL", ""),
        env.get("WASSUP_PATIENTS_CLINIC", ""),
    )
    if not admin_url or not slug:
        print("WASSUP_ADMIN_DATABASE_URL and WASSUP_PATIENTS_CLINIC are required", file=sys.stderr)
        return 2
    apply = env.get("WASSUP_PATIENTS_APPLY") == "true"
    production = env.get("WASSUP_ENVIRONMENT", "") not in ("local", "test", "staging")
    if apply and production and env.get("WASSUP_PRODUCTION_ACK") != slug:
        print(
            "import refused: applying to production needs WASSUP_PRODUCTION_ACK set to the "
            f"clinic slug ({slug!r})",
            file=sys.stderr,
        )
        return 2
    try:
        data = read_file(
            env.get("WASSUP_PATIENTS_PATH"),
            env.get("WASSUP_PATIENTS_URL"),
            env.get("WASSUP_PATIENTS_SHA256"),
        )
        counts = run(admin_url, slug, data, apply=apply)
    except (ImportRefused, UnicodeDecodeError, OSError) as exc:
        print(f"import refused: {exc}", file=sys.stderr)
        return 1
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    print(
        "committed" if apply else "dry run: verified, then rolled back (WASSUP_PATIENTS_APPLY=true)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
