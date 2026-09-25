"""Create the database roles and their passwords (run once per database, idempotent).

Runs ``db/roles.sql`` and ``db/grant_database.sql`` as the database admin, then sets each login
role's password from the environment. It is designed to run *inside* the platform (a one-shot
``db-admin`` service on Railway) with passwords the platform generates, so no password is ever
typed, printed or stored in the repository:

    WASSUP_ADMIN_DATABASE_URL   admin (superuser) URL of the database
    WASSUP_PASSWORD_MIGRATOR    password for wassup_migrator
    WASSUP_PASSWORD_APP_VOICE   password for app_voice
    WASSUP_PASSWORD_APP_CORE    password for app_core
    WASSUP_PASSWORD_APP_OPS     password for app_ops

Output is role names and outcomes only. Exit status is non-zero on any failure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql

HERE = Path(__file__).resolve().parent
LOGIN_ROLES = {
    "wassup_migrator": "WASSUP_PASSWORD_MIGRATOR",
    "app_voice": "WASSUP_PASSWORD_APP_VOICE",
    "app_core": "WASSUP_PASSWORD_APP_CORE",
    "app_ops": "WASSUP_PASSWORD_APP_OPS",
}
MIN_PASSWORD_LENGTH = 24


def _url(raw: str) -> str:
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix) :]
    return raw


def bootstrap(admin_url: str, passwords: dict[str, str]) -> None:
    for role, value in passwords.items():
        if len(value) < MIN_PASSWORD_LENGTH:
            raise SystemExit(
                f"password for {role} is missing or shorter than {MIN_PASSWORD_LENGTH}"
            )
    with psycopg.connect(_url(admin_url), autocommit=True) as conn:
        dbname = conn.execute("SELECT current_database()").fetchone()[0]  # type: ignore[index]
        # Raw files go to the server as-is (no client-side parameters): '%' in format() strings
        # must not be mistaken for placeholders.
        conn.execute((HERE / "roles.sql").read_text())
        grants = (
            (HERE / "grant_database.sql")
            .read_text()
            .replace(':"dbname"', sql.Identifier(dbname).as_string(conn))
        )
        conn.execute(grants)
        for role, value in passwords.items():
            conn.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(value)
                )
            )
            print(f"role {role}: password set")
    print(f"bootstrap complete for database {dbname}")


def main() -> int:
    admin_url = os.environ.get("WASSUP_ADMIN_DATABASE_URL", "")
    if not admin_url:
        print("WASSUP_ADMIN_DATABASE_URL is not set", file=sys.stderr)
        return 2
    passwords = {role: os.environ.get(var, "") for role, var in LOGIN_ROLES.items()}
    bootstrap(admin_url, passwords)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
