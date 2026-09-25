"""Create a clinic and its first owner (run inside the platform as ``db-admin``).

Everything else about a clinic's people is done from the dashboard's Team page, but a new
clinic has nobody who could invite anyone: this tool creates the clinic and makes its first
owner a member directly. It can also map the clinic's voice agents and phone numbers, so calls
route to it once its agent points at this platform.

    WASSUP_ROLE=onboard-clinic
    WASSUP_ADMIN_DATABASE_URL       this database (admin URL)
    WASSUP_CLINIC_SLUG              short id used in tool URLs, e.g. regenu (a-z, 0-9, dashes)
    WASSUP_CLINIC_NAME              display name
    WASSUP_CLINIC_STATE             NSW, VIC, QLD, WA, SA, TAS, ACT or NT (public holidays)
    WASSUP_CLINIC_TIMEZONE          IANA zone, Australia/* (default Australia/Sydney)
    WASSUP_CLINIC_ORGANIZATION      the business the clinic belongs to (default: the name)
    WASSUP_CLINIC_OWNER_UID         the first owner's sign-in account id (Firebase console,
                                    Authentication → Users → User UID)
    WASSUP_CLINIC_OWNER_EMAIL       the first owner's sign-in email
    WASSUP_CLINIC_AGENT_IDS         optional, comma-separated voice agent ids
    WASSUP_CLINIC_NUMBERS           optional, comma-separated phone numbers the agents answer
    WASSUP_CLINIC_ALERT_EMAILS      optional, comma-separated urgent-message recipients
    WASSUP_CLINIC_APPLY             "true" to commit; otherwise a dry run that checks everything
    WASSUP_PRODUCTION_ACK           in production, an apply also needs this set to the slug

Run again with the same slug to add agents, numbers or the owner to a clinic created earlier;
the name, state and timezone must then match (changing a clinic is not this tool's job). An
agent id or number already mapped to a different clinic refuses the whole run. Output is ids
and counts; the owner's email is not printed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.rows import dict_row
from wassup_core.phone import canonical_phone

STATES = ("NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
UID_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
AGENT_RE = re.compile(r"^[A-Za-z0-9_-]{3,200}$")


class OnboardRefused(Exception):
    pass


class _DryRun(Exception):
    pass


def _url(raw: str) -> str:
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix) :]
    host = urlsplit(raw).hostname or ""
    local = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".railway.internal")
    if not local and "sslmode=" not in raw:
        raw += ("&" if "?" in raw else "?") + "sslmode=require"
    return raw


def _list(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def plan(env: Mapping[str, str]) -> dict[str, Any]:
    """Validate the variables into a plan, before touching the database."""
    slug = env.get("WASSUP_CLINIC_SLUG", "").strip()
    name = env.get("WASSUP_CLINIC_NAME", "").strip()
    state = env.get("WASSUP_CLINIC_STATE", "").strip().upper()
    zone = env.get("WASSUP_CLINIC_TIMEZONE", "Australia/Sydney").strip()
    uid = env.get("WASSUP_CLINIC_OWNER_UID", "").strip()
    email = env.get("WASSUP_CLINIC_OWNER_EMAIL", "").strip()
    if not SLUG_RE.match(slug) or len(slug) > 40:
        raise OnboardRefused("WASSUP_CLINIC_SLUG: lower-case letters, digits and dashes")
    if not name or len(name) > 120:
        raise OnboardRefused("WASSUP_CLINIC_NAME is required (up to 120 characters)")
    if state not in STATES:
        raise OnboardRefused(f"WASSUP_CLINIC_STATE must be one of {', '.join(STATES)}")
    if not zone.startswith("Australia/"):
        raise OnboardRefused("WASSUP_CLINIC_TIMEZONE must be an Australia/* zone")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise OnboardRefused(f"unknown timezone {zone!r}") from exc
    if not UID_RE.match(uid):
        raise OnboardRefused("WASSUP_CLINIC_OWNER_UID: the owner's sign-in account id")
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise OnboardRefused("WASSUP_CLINIC_OWNER_EMAIL: the owner's sign-in email")
    agents = _list(env.get("WASSUP_CLINIC_AGENT_IDS", ""))
    if not all(AGENT_RE.match(a) for a in agents):
        raise OnboardRefused("WASSUP_CLINIC_AGENT_IDS: comma-separated agent ids")
    numbers = []
    for raw in _list(env.get("WASSUP_CLINIC_NUMBERS", "")):
        number = canonical_phone(raw)
        if number is None:
            raise OnboardRefused(f"WASSUP_CLINIC_NUMBERS: {raw!r} is not a usable number")
        numbers.append(number)
    alerts = _list(env.get("WASSUP_CLINIC_ALERT_EMAILS", ""))
    if not all(EMAIL_RE.match(a) for a in alerts):
        raise OnboardRefused("WASSUP_CLINIC_ALERT_EMAILS: comma-separated emails")
    environment = env.get("WASSUP_ENVIRONMENT", "")
    return {
        "slug": slug,
        "name": name,
        "state": state,
        "timezone": zone,
        "organization": env.get("WASSUP_CLINIC_ORGANIZATION", "").strip() or name,
        "owner_uid": uid,
        "owner_email": email,
        "agents": sorted(set(agents)),
        "numbers": sorted(set(numbers)),
        "alerts": alerts,
        "agent_environment": "staging"
        if environment in ("staging", "local", "test")
        else "production",
    }


def _clinic(conn: psycopg.Connection[Any], p: dict[str, Any]) -> tuple[uuid.UUID, bool]:
    row = conn.execute(
        "SELECT id, name, state, timezone FROM clinics WHERE slug = %s", (p["slug"],)
    ).fetchone()
    if row is not None:
        if (row["name"], row["state"], row["timezone"]) != (p["name"], p["state"], p["timezone"]):
            raise OnboardRefused(
                f"clinic {p['slug']!r} exists with a different name, state or timezone; "
                "nothing changed"
            )
        return uuid.UUID(str(row["id"])), False
    org = conn.execute(
        "INSERT INTO organizations (name) VALUES (%s) RETURNING id", (p["organization"],)
    ).fetchone()
    clinic_id = uuid.uuid4()
    conn.execute("SELECT set_config('app.clinic_ids', %s, true)", (f"{{{clinic_id}}}",))
    conn.execute(
        "INSERT INTO clinics (id, organization_id, slug, name, state, timezone, alert_contacts) "
        "VALUES (%s, %s, %s, %s, %s, %s, CAST(%s AS jsonb))",
        (
            clinic_id,
            org["id"],
            p["slug"],
            p["name"],
            p["state"],
            p["timezone"],
            json.dumps(p["alerts"]),
        ),
    )
    return clinic_id, True


def _refuse_if_elsewhere(
    conn: psycopg.Connection[Any], table: str, column: str, values: list[str], clinic_id: uuid.UUID
) -> None:
    if not values:
        return
    query = {
        (
            "clinic_voice_agents",
            "agent_id",
        ): "SELECT agent_id AS v, clinic_id FROM clinic_voice_agents WHERE agent_id = ANY(%s)",
        (
            "clinic_phone_numbers",
            "e164",
        ): "SELECT e164 AS v, clinic_id FROM clinic_phone_numbers WHERE e164 = ANY(%s)",
    }[(table, column)]
    # Admin connection outside any clinic scope: row-level security would hide other clinics.
    taken = [r["v"] for r in conn.execute(query, (values,)) if r["clinic_id"] != clinic_id]
    if taken:
        raise OnboardRefused(
            f"already mapped to another clinic: {', '.join(taken)}; nothing changed"
        )


def run(admin_url: str, env: Mapping[str, str], *, apply: bool) -> dict[str, Any]:
    p = plan(env)
    with psycopg.connect(_url(admin_url), row_factory=dict_row) as conn:
        result: dict[str, Any] = {"slug": p["slug"], "applied": apply}
        try:
            with conn.transaction():
                conn.execute("SET LOCAL lock_timeout = '5s'")
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('onboard-clinic:' || %s))", (p["slug"],)
                )
                existing = conn.execute(
                    "SELECT id FROM clinics WHERE slug = %s", (p["slug"],)
                ).fetchone()
                if existing is not None:
                    _refuse_if_elsewhere(
                        conn,
                        "clinic_voice_agents",
                        "agent_id",
                        p["agents"],
                        uuid.UUID(str(existing["id"])),
                    )
                    _refuse_if_elsewhere(
                        conn,
                        "clinic_phone_numbers",
                        "e164",
                        p["numbers"],
                        uuid.UUID(str(existing["id"])),
                    )
                else:
                    _refuse_if_elsewhere(
                        conn, "clinic_voice_agents", "agent_id", p["agents"], uuid.UUID(int=0)
                    )
                    _refuse_if_elsewhere(
                        conn, "clinic_phone_numbers", "e164", p["numbers"], uuid.UUID(int=0)
                    )
                conn.execute("SET LOCAL ROLE wassup_owner")
                clinic_id, created = _clinic(conn, p)
                conn.execute("SELECT set_config('app.clinic_ids', %s, true)", (f"{{{clinic_id}}}",))
                agents = sum(
                    conn.execute(
                        "INSERT INTO clinic_voice_agents (clinic_id, agent_id, environment) "
                        "VALUES (%s, %s, %s) ON CONFLICT (provider, agent_id) DO NOTHING",
                        (clinic_id, agent, p["agent_environment"]),
                    ).rowcount
                    for agent in p["agents"]
                )
                numbers = sum(
                    conn.execute(
                        "INSERT INTO clinic_phone_numbers (clinic_id, e164) VALUES (%s, %s) "
                        "ON CONFLICT (e164) DO NOTHING",
                        (clinic_id, number),
                    ).rowcount
                    for number in p["numbers"]
                )
                staff = conn.execute(
                    "INSERT INTO staff_users (firebase_uid, email) VALUES (%s, %s) "
                    "ON CONFLICT (firebase_uid) DO UPDATE SET email = EXCLUDED.email RETURNING id",
                    (p["owner_uid"], p["owner_email"]),
                ).fetchone()
                member = conn.execute(
                    "INSERT INTO clinic_memberships (clinic_id, staff_user_id, role) "
                    "VALUES (%s, %s, 'owner') ON CONFLICT (clinic_id, staff_user_id) "
                    "DO UPDATE SET role = 'owner' RETURNING (xmax = 0) AS inserted",
                    (clinic_id, staff["id"]),
                ).fetchone()
                conn.execute(
                    "INSERT INTO audit_log (clinic_id, actor_service, action, target_type, "
                    "target_id, detail) VALUES (%s, 'db-admin', 'clinic.onboarded', 'clinic', %s, "
                    "CAST(%s AS jsonb))",
                    (
                        clinic_id,
                        str(clinic_id),
                        json.dumps(
                            {
                                "created": created,
                                "agents_added": agents,
                                "numbers_added": numbers,
                                "owner_staff_user_id": str(staff["id"]),
                            }
                        ),
                    ),
                )
                result.update(
                    clinic_id=str(clinic_id),
                    created=created,
                    agents_added=agents,
                    numbers_added=numbers,
                    owner_staff_user_id=str(staff["id"]),
                    owner_membership="added" if member["inserted"] else "made owner",
                )
                if not apply:
                    raise _DryRun
        except _DryRun:
            pass
    return result


def main() -> int:
    env = os.environ
    admin_url = env.get("WASSUP_ADMIN_DATABASE_URL", "")
    if not admin_url:
        print("WASSUP_ADMIN_DATABASE_URL is required", file=sys.stderr)
        return 2
    apply = env.get("WASSUP_CLINIC_APPLY") == "true"
    production = env.get("WASSUP_ENVIRONMENT", "") not in ("local", "test", "staging")
    slug = env.get("WASSUP_CLINIC_SLUG", "")
    if apply and production and env.get("WASSUP_PRODUCTION_ACK") != slug:
        print(
            f"refused: applying in production needs WASSUP_PRODUCTION_ACK set to the slug ({slug!r})",
            file=sys.stderr,
        )
        return 2
    try:
        result = run(admin_url, env, apply=apply)
    except OnboardRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:  # never echo a database message: it can quote a row
        print(f"refused: database error {type(exc).__name__}", file=sys.stderr)
        return 1
    print(" ".join(f"{k}={v}" for k, v in result.items()))
    print("committed" if apply else "dry run: checked, then rolled back (WASSUP_CLINIC_APPLY=true)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
