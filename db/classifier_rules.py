"""Operator tools for a clinic's classification rules (run inside the platform as ``db-admin``).

    WASSUP_ROLE=load-classifier-rules   store a new rules version for one clinic, validated
                                        first, inactive until activated (or activated at once
                                        with WASSUP_RULES_ACTIVATE=true); then reclassify
    WASSUP_ROLE=activate-classifier-rules  switch a clinic to an existing version (the rollback)
    WASSUP_ROLE=reclassify              recompute every call of one clinic (or all clinics with
                                        active rules) with its active version

    WASSUP_ADMIN_DATABASE_URL   this system's database (admin URL)
    WASSUP_RULES_CLINIC         clinic slug (required for load/activate; optional for reclassify)
    WASSUP_RULES_JSON           the rules document (load only). It is clinic vocabulary, not a
                                secret, but it is never printed and never committed here.
    WASSUP_RULES_NOTE           optional note stored with the version (load only)
    WASSUP_RULES_ACTIVATE       "true" to activate the new version immediately (load only)
    WASSUP_RULES_VERSION        the version to activate (activate only)

Reclassification is idempotent and auditable: every call gets ``priority_level``,
``is_priority``, ``is_reception_action``, ``action_label``, ``classified_at`` and
``classifier_version`` from the active rules, in one transaction per clinic, as the owner role
under that clinic's row-level security. Output is counts only.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row
from wassup_core.classify import CallFacts, RuleSet, classify

_CALLS = """
    SELECT id, summary, transcript, intent, sentiment, triage_route, call_successful,
           priority_level, is_priority, is_reception_action, action_label, classifier_version
    FROM calls WHERE clinic_id = %(c)s AND analyzed_at IS NOT NULL ORDER BY id
"""
_UPDATE = """
    UPDATE calls SET priority_level = %(level)s, priority_reason = %(reason)s,
        is_priority = %(is_priority)s, is_reception_action = %(is_reception_action)s,
        action_label = %(action_label)s, classified_at = now(), classifier_version = %(version)s,
        updated_at = now()
    WHERE id = %(id)s
"""


def _url(raw: str) -> str:
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix) :]
    return raw


def _scope(conn: psycopg.Connection[Any], clinic_id: uuid.UUID) -> None:
    conn.execute("SET LOCAL ROLE wassup_owner")
    conn.execute("SELECT set_config('app.clinic_ids', %s, true)", (f"{{{clinic_id}}}",))


def _clinic_id(conn: psycopg.Connection[Any], slug: str) -> uuid.UUID:
    row = conn.execute("SELECT id FROM clinics WHERE slug = %s", (slug,)).fetchone()
    if row is None:
        raise SystemExit(f"no clinic with slug {slug!r}")
    return uuid.UUID(str(row["id"]))


def load(
    conn: psycopg.Connection[Any], slug: str, raw: str, note: str | None, activate: bool
) -> int:
    rules = RuleSet.parse(raw)  # refuses anything the engine can't read, before any write
    clinic_id = _clinic_id(conn, slug)
    with conn.transaction():
        _scope(conn, clinic_id)
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('classifier-rules:' || %s))", (str(clinic_id),)
        )
        version = int(
            conn.execute(
                "SELECT coalesce(max(version), 0) + 1 AS v FROM clinic_classifier_rules WHERE clinic_id = %s",
                (clinic_id,),
            ).fetchone()["v"]
        )
        if activate:
            conn.execute(
                "UPDATE clinic_classifier_rules SET active = false WHERE clinic_id = %s",
                (clinic_id,),
            )
        conn.execute(
            "INSERT INTO clinic_classifier_rules (clinic_id, version, rules, active, note) "
            "VALUES (%s, %s, %s, %s, %s)",
            (clinic_id, version, json.dumps(rules.model_dump()), activate, note),
        )
    print(f"clinic {slug}: rules version {version} stored{' and activated' if activate else ''}")
    return version


def activate(conn: psycopg.Connection[Any], slug: str, version: int) -> None:
    clinic_id = _clinic_id(conn, slug)
    with conn.transaction():
        _scope(conn, clinic_id)
        conn.execute(
            "UPDATE clinic_classifier_rules SET active = false WHERE clinic_id = %s", (clinic_id,)
        )
        updated = conn.execute(
            "UPDATE clinic_classifier_rules SET active = true WHERE clinic_id = %s AND version = %s",
            (clinic_id, version),
        ).rowcount
        if updated != 1:
            raise SystemExit(f"clinic {slug} has no rules version {version}; nothing changed")
    print(f"clinic {slug}: rules version {version} active")


def reclassify(conn: psycopg.Connection[Any], slug: str | None) -> dict[str, int]:
    """Recompute every analysed call of the clinic(s) with their active rules. Counts only."""
    if slug is None:
        clinics = [
            (r["slug"], uuid.UUID(str(r["id"])))
            for r in conn.execute(
                "SELECT c.slug, c.id FROM clinics c WHERE EXISTS "
                "(SELECT 1 FROM clinic_classifier_rules r WHERE r.clinic_id = c.id AND r.active)"
            )
        ]
    else:
        clinics = [(slug, _clinic_id(conn, slug))]
    totals = {"clinics": 0, "calls": 0, "changed": 0, "unchanged": 0, "no_active_rules": 0}
    for name, clinic_id in clinics:
        with conn.transaction():
            _scope(conn, clinic_id)
            active = conn.execute(
                "SELECT version, rules FROM clinic_classifier_rules WHERE clinic_id = %s AND active",
                (clinic_id,),
            ).fetchone()
            if active is None:
                totals["no_active_rules"] += 1
                print(f"clinic {name}: no active rules, skipped")
                continue
            rules = RuleSet.parse(active["rules"])
            version = int(active["version"])
            changed = unchanged = 0
            for row in conn.execute(_CALLS, {"c": clinic_id}).fetchall():
                result = classify(
                    CallFacts(
                        summary=row["summary"],
                        transcript=row["transcript"],
                        intent=row["intent"],
                        sentiment=row["sentiment"],
                        triage_route=row["triage_route"],
                        call_successful=row["call_successful"],
                    ),
                    rules,
                )
                same = (
                    row["priority_level"] == result.level
                    and row["is_priority"] == result.is_priority
                    and row["is_reception_action"] == result.is_reception_action
                    and row["action_label"] == result.action_label[:60]
                    and row["classifier_version"] == version
                )
                if same:
                    unchanged += 1
                    continue
                conn.execute(
                    _UPDATE,
                    {
                        "id": row["id"],
                        "level": result.level,
                        "reason": result.reason[:60],
                        "is_priority": result.is_priority,
                        "is_reception_action": result.is_reception_action,
                        "action_label": result.action_label[:60],
                        "version": version,
                    },
                )
                changed += 1
            conn.execute(
                "INSERT INTO audit_log (clinic_id, actor_service, action, target_type, target_id, detail) "
                "VALUES (%s, 'db-admin', 'calls.reclassify', 'clinic', %s, CAST(%s AS jsonb))",
                (
                    clinic_id,
                    str(clinic_id),
                    json.dumps({"version": version, "changed": changed, "unchanged": unchanged}),
                ),
            )
        totals["clinics"] += 1
        totals["calls"] += changed + unchanged
        totals["changed"] += changed
        totals["unchanged"] += unchanged
        print(f"clinic {name}: version {version}, {changed} changed, {unchanged} unchanged")
    return totals


def main() -> int:
    env = os.environ
    role = env.get("WASSUP_ROLE", "")
    admin_url = env.get("WASSUP_ADMIN_DATABASE_URL", "")
    if not admin_url:
        print("WASSUP_ADMIN_DATABASE_URL is not set", file=sys.stderr)
        return 2
    slug = env.get("WASSUP_RULES_CLINIC") or None
    try:
        with psycopg.connect(_url(admin_url), row_factory=dict_row) as conn:
            if role == "load-classifier-rules":
                if not slug or not env.get("WASSUP_RULES_JSON"):
                    print("WASSUP_RULES_CLINIC and WASSUP_RULES_JSON are required", file=sys.stderr)
                    return 2
                load(
                    conn,
                    slug,
                    env["WASSUP_RULES_JSON"],
                    env.get("WASSUP_RULES_NOTE") or None,
                    env.get("WASSUP_RULES_ACTIVATE") == "true",
                )
                if env.get("WASSUP_RULES_ACTIVATE") == "true":
                    reclassify(conn, slug)
            elif role == "activate-classifier-rules":
                if not slug or not env.get("WASSUP_RULES_VERSION", "").isdigit():
                    print(
                        "WASSUP_RULES_CLINIC and WASSUP_RULES_VERSION are required", file=sys.stderr
                    )
                    return 2
                activate(conn, slug, int(env["WASSUP_RULES_VERSION"]))
                reclassify(conn, slug)
            elif role == "reclassify":
                totals = reclassify(conn, slug)
                print(" ".join(f"{k}={v}" for k, v in totals.items()))
            else:
                print(f"unknown WASSUP_ROLE for classifier rules: {role!r}", file=sys.stderr)
                return 2
    except ValueError as exc:  # invalid rules document: nothing was written
        print(f"rules refused: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
