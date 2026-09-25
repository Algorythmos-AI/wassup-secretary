"""Seed a synthetic clinic into a non-production database (idempotent).

For staging smoke tests and demos. Everything is obviously synthetic (".example.test" emails,
+614000009xx numbers). Refuses to run when WASSUP_ENVIRONMENT is production.

    WASSUP_ADMIN_DATABASE_URL   admin URL (row-level security applies to the owner role too, so
                                rows are written as the owner with an explicit clinic context)
    WASSUP_SEED_STAFF           optional "firebase_uid:email:role,…" memberships to add
"""

from __future__ import annotations

import os
import sys
import uuid

import psycopg

CLINIC_ID = uuid.UUID("5ea1c11c-0000-4000-8000-000000000001")
ORG_ID = uuid.UUID("5ea1c11c-0000-4000-8000-0000000000aa")
AGENT_ID = "agent_staging_synthetic"
NUMBER = "+61400000999"
ROLES = {"viewer", "receptionist", "admin", "owner"}


def _url(raw: str) -> str:
    return "postgresql://" + raw.split("://", 1)[1] if "://" in raw else raw


def seed(admin_url: str, staff: list[tuple[str, str, str]]) -> None:
    with psycopg.connect(_url(admin_url)) as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE wassup_owner")
        conn.execute("SELECT set_config('app.clinic_ids', %s, true)", ("{" + str(CLINIC_ID) + "}",))
        conn.execute(
            "INSERT INTO organizations (id, name) VALUES (%s, 'Staging Synthetic Org') "
            "ON CONFLICT (id) DO NOTHING",
            (ORG_ID,),
        )
        conn.execute(
            "INSERT INTO clinics (id, organization_id, slug, name, state, timezone, alert_contacts) "
            "VALUES (%s, %s, 'staging-clinic', 'Staging Synthetic Clinic', 'NSW', 'Australia/Sydney', "
            "'[\"alerts@staging.example.test\"]') ON CONFLICT (id) DO NOTHING",
            (CLINIC_ID, ORG_ID),
        )
        conn.execute(
            "INSERT INTO clinic_voice_agents (clinic_id, agent_id, environment) "
            "VALUES (%s, %s, 'staging') ON CONFLICT (provider, agent_id) DO NOTHING",
            (CLINIC_ID, AGENT_ID),
        )
        conn.execute(
            "INSERT INTO clinic_phone_numbers (clinic_id, e164) VALUES (%s, %s) "
            "ON CONFLICT (e164) DO NOTHING",
            (CLINIC_ID, NUMBER),
        )
        conn.execute("RESET ROLE")
        for uid, email, role in staff:
            staff_id = conn.execute(
                "INSERT INTO staff_users (firebase_uid, email) VALUES (%s, %s) "
                "ON CONFLICT (firebase_uid) DO UPDATE SET email = EXCLUDED.email RETURNING id",
                (uid, email),
            ).fetchone()[0]  # type: ignore[index]
            conn.execute(
                "INSERT INTO clinic_memberships (clinic_id, staff_user_id, role) VALUES (%s, %s, %s) "
                "ON CONFLICT (clinic_id, staff_user_id) DO UPDATE SET role = EXCLUDED.role",
                (CLINIC_ID, staff_id, role),
            )
    print(
        f"synthetic clinic {CLINIC_ID} ready (agent {AGENT_ID}, number {NUMBER}, {len(staff)} staff)"
    )


def parse_staff(raw: str) -> list[tuple[str, str, str]]:
    staff = []
    for item in filter(None, (s.strip() for s in raw.split(","))):
        uid, email, role = item.split(":", 2)
        if role not in ROLES:
            raise SystemExit(f"unknown role {role!r}")
        staff.append((uid, email, role))
    return staff


def main() -> int:
    if os.environ.get("WASSUP_ENVIRONMENT", "production") == "production":
        print("refusing to seed synthetic data into production", file=sys.stderr)
        return 2
    seed(
        os.environ["WASSUP_ADMIN_DATABASE_URL"],
        parse_staff(os.environ.get("WASSUP_SEED_STAFF", "")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
