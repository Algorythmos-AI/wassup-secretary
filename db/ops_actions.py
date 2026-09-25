"""Operator decisions the runbooks call for, without a SQL session (run as ``db-admin``).

The database is on the private network only, so nobody opens an admin SQL session to it. The
outbox-dead-letter and quarantine runbooks' decisions are made here instead, one per run, driven
by variables, printed as ids and codes only, and audited:

    WASSUP_ROLE=ops
    WASSUP_ADMIN_DATABASE_URL     this database (admin URL)
    WASSUP_OPS_ACTION             list | requeue-outbox | abandon-outbox | resolve-quarantine |
                                  requeue-quarantined-webhooks
    WASSUP_OPS_IDS                comma-separated ids (outbox events, or quarantine records)
    WASSUP_OPS_AGENT              the agent id (requeue-quarantined-webhooks)
    WASSUP_OPS_RESOLUTION         replayed | not_ours | handled_by_phone (resolve-quarantine)
    WASSUP_OPS_APPLY              "true" to commit; otherwise a dry run that shows what would change
    WASSUP_PRODUCTION_ACK         in production, an apply also needs this set to the action name

``list`` shows dead outbox events and unresolved quarantine records (ids, clinics, types, error
codes, times; never payloads). An id that is not in the expected state refuses the whole run.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

ACTIONS = (
    "list",
    "requeue-outbox",
    "abandon-outbox",
    "resolve-quarantine",
    "requeue-quarantined-webhooks",
)
RESOLUTIONS = ("replayed", "not_ours", "handled_by_phone")


class OpsRefused(Exception):
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


def _ids(raw: str) -> list[int]:
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    if not ids or not all(re.fullmatch(r"[0-9]{1,18}", i) for i in ids):
        raise OpsRefused("WASSUP_OPS_IDS must be a comma-separated list of numeric ids")
    return sorted({int(i) for i in ids})


def _audit(
    conn: psycopg.Connection[Any], clinic_id: Any, action: str, target: str, detail: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO audit_log (clinic_id, actor_service, action, target_type, target_id, detail) "
        "VALUES (%s, 'db-admin', %s, 'outbox_event', %s, CAST(%s AS jsonb))",
        (clinic_id, action, target, json.dumps(detail)),
    )


def listing(conn: psycopg.Connection[Any]) -> dict[str, list[dict[str, Any]]]:
    dead = conn.execute(
        "SELECT id, clinic_id, event_type, attempts, last_error, created_at "
        "FROM outbox_events WHERE status = 'dead' ORDER BY id"
    ).fetchall()
    quarantined = conn.execute(
        "SELECT id, reason, agent_id, received_at FROM quarantine_events "
        "WHERE resolved_at IS NULL ORDER BY id"
    ).fetchall()
    return {"dead_outbox": dead, "unresolved_quarantine": quarantined}


def _outbox(conn: psycopg.Connection[Any], ids: list[int], new_status: str) -> list[int]:
    found = conn.execute(
        "SELECT id, clinic_id FROM outbox_events WHERE id = ANY(%s) AND status = 'dead' "
        "ORDER BY id FOR UPDATE",
        (ids,),
    ).fetchall()
    if [r["id"] for r in found] != ids:
        missing = sorted(set(ids) - {r["id"] for r in found})
        raise OpsRefused(f"not dead-lettered (or no such event): {missing}; nothing changed")
    for row in found:
        if new_status == "pending":
            conn.execute(
                "UPDATE outbox_events SET status = 'pending', attempts = 0, available_at = now(), "
                "last_error = NULL WHERE id = %s",
                (row["id"],),
            )
        else:
            conn.execute(
                "UPDATE outbox_events SET status = 'abandoned' WHERE id = %s", (row["id"],)
            )
        _audit(
            conn,
            row["clinic_id"],
            "outbox.requeued" if new_status == "pending" else "outbox.abandoned",
            str(row["id"]),
            {"by": "db-admin"},
        )
    return ids


def _resolve_quarantine(
    conn: psycopg.Connection[Any], ids: list[int], resolution: str
) -> list[int]:
    if resolution not in RESOLUTIONS:
        raise OpsRefused(f"WASSUP_OPS_RESOLUTION must be one of {', '.join(RESOLUTIONS)}")
    found = conn.execute(
        "SELECT id FROM quarantine_events WHERE id = ANY(%s) AND resolved_at IS NULL "
        "ORDER BY id FOR UPDATE",
        (ids,),
    ).fetchall()
    if [r["id"] for r in found] != ids:
        missing = sorted(set(ids) - {r["id"] for r in found})
        raise OpsRefused(f"not unresolved (or no such record): {missing}; nothing changed")
    conn.execute(
        "UPDATE quarantine_events SET resolved_at = now(), resolution = %s WHERE id = ANY(%s)",
        (resolution, ids),
    )
    return ids


def _requeue_webhooks(conn: psycopg.Connection[Any], agent: str) -> int:
    if not agent or len(agent) > 200:
        raise OpsRefused("WASSUP_OPS_AGENT is required")
    rows = conn.execute(
        "UPDATE retell_events_raw SET error = 'processing_failed:requeued', replay_attempts = 0 "
        "WHERE error LIKE 'quarantined:%%' AND processed_at IS NULL AND agent_id = %s RETURNING id",
        (agent,),
    ).fetchall()
    return len(rows)


def run(admin_url: str, env: Mapping[str, str], *, apply: bool) -> dict[str, Any]:
    action = env.get("WASSUP_OPS_ACTION", "list")
    if action not in ACTIONS:
        raise OpsRefused(f"WASSUP_OPS_ACTION must be one of {', '.join(ACTIONS)}")
    with psycopg.connect(_url(admin_url), row_factory=dict_row) as conn:
        if action == "list":
            return {"action": action, **listing(conn)}
        result: dict[str, Any] = {"action": action, "applied": apply}
        try:
            with conn.transaction():
                conn.execute("SET LOCAL lock_timeout = '5s'")
                if action in ("requeue-outbox", "abandon-outbox"):
                    status = "pending" if action == "requeue-outbox" else "abandoned"
                    result["ids"] = _outbox(conn, _ids(env.get("WASSUP_OPS_IDS", "")), status)
                elif action == "resolve-quarantine":
                    result["ids"] = _resolve_quarantine(
                        conn,
                        _ids(env.get("WASSUP_OPS_IDS", "")),
                        env.get("WASSUP_OPS_RESOLUTION", ""),
                    )
                else:
                    result["events"] = _requeue_webhooks(conn, env.get("WASSUP_OPS_AGENT", ""))
                if not apply:
                    raise _DryRun
        except _DryRun:
            pass
        return result


def _print(result: dict[str, Any]) -> None:
    for key, value in result.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            print(f"{key}: {len(value)}")
            for row in value:
                print("  " + " ".join(f"{k}={v}" for k, v in row.items()))
        else:
            print(f"{key}: {value}")


def main() -> int:
    env = os.environ
    admin_url = env.get("WASSUP_ADMIN_DATABASE_URL", "")
    if not admin_url:
        print("WASSUP_ADMIN_DATABASE_URL is required", file=sys.stderr)
        return 2
    action = env.get("WASSUP_OPS_ACTION", "list")
    apply = env.get("WASSUP_OPS_APPLY") == "true"
    production = env.get("WASSUP_ENVIRONMENT", "") not in ("local", "test", "staging")
    if apply and production and action != "list" and env.get("WASSUP_PRODUCTION_ACK") != action:
        print(
            f"refused: applying in production needs WASSUP_PRODUCTION_ACK set to {action!r}",
            file=sys.stderr,
        )
        return 2
    try:
        result = run(admin_url, env, apply=apply)
    except OpsRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:  # never echo a database message: it can quote a row
        print(f"refused: database error {type(exc).__name__}", file=sys.stderr)
        return 1
    _print(result)
    if action != "list":
        print(
            "committed" if apply else "dry run: checked, then rolled back (WASSUP_OPS_APPLY=true)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
