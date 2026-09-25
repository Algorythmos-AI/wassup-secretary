"""Counts-only health report of a database (no personal data): what landed where.

WASSUP_ADMIN_DATABASE_URL   admin URL (superuser: row-level security doesn't apply to it)
"""

from __future__ import annotations

import os

import psycopg

QUERIES = {
    "clinics": "SELECT count(*) FROM clinics",
    "calls": "SELECT count(*) FROM calls",
    "calls_analyzed": "SELECT count(*) FROM calls WHERE analyzed_at IS NOT NULL",
    "messages": "SELECT count(*) FROM messages",
    "urgent_messages": "SELECT count(*) FROM messages WHERE urgent",
    "tool_invocations": "SELECT count(*) FROM tool_invocations",
    "raw_events_open": "SELECT count(*) FROM retell_events_raw WHERE processed_at IS NULL",
    "quarantine_open": "SELECT count(*) FROM quarantine_events WHERE resolved_at IS NULL",
    "audit_rows": "SELECT count(*) FROM audit_log",
    "schema_revision": "SELECT version_num FROM alembic_version",
}
OUTBOX = "SELECT event_type, status, count(*) FROM outbox_events GROUP BY 1, 2 ORDER BY 1, 2"


def main() -> int:
    url = os.environ["WASSUP_ADMIN_DATABASE_URL"]
    with psycopg.connect("postgresql://" + url.split("://", 1)[1]) as conn:
        for name, query in QUERIES.items():
            print(f"{name}: {conn.execute(query).fetchone()[0]}")  # type: ignore[index]
        for event_type, status, n in conn.execute(OUTBOX):
            print(f"outbox {event_type} {status}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
