"""Import one clinic's history from the legacy dashboard database (ADR 0009).

Copies the legacy ``calls``, ``w1_messages``, ``w1_promises`` and ``call_interactions`` rows that
belong to one clinic into this system's tables, marked ``source = 'import'``. It is designed to run
*inside* the platform (a one-shot ``db-admin`` service, ``WASSUP_ROLE=import-legacy``), so patient
data goes database to database and never through anyone's laptop.

    WASSUP_LEGACY_DATABASE_URL    legacy database; read in one READ ONLY snapshot, never written
    WASSUP_ADMIN_DATABASE_URL     this system's database (admin URL)
    WASSUP_IMPORT_CLINIC          target clinic slug (the clinic must already exist)
    WASSUP_IMPORT_AGENTS          comma-separated legacy agent ids whose calls are this clinic's
    WASSUP_IMPORT_PRACTICE_NAMES  optional, "|"-separated legacy ``practice`` values, for old rows
                                  written before calls carried an agent id
    WASSUP_IMPORT_ORPHANS         "true" to also import messages and promises whose call never
                                  arrived (its webhook was lost), under a placeholder call so they
                                  show up in the inbox; only when every legacy message and promise
                                  belongs to this clinic
    WASSUP_IMPORT_APPLY           "true" to commit; anything else is a dry run that does all the
                                  work, verifies it, then rolls back

Guarantees:
- **Idempotent.** Calls are keyed on (clinic, provider call id); messages, promises and history
  on ``legacy:<table>:<id>`` keys. Running it again adds only what is new.
- **Never overwrites live data.** A call already ingested by the webhook is left alone. An
  imported call is refreshed on a re-run (for example, the legacy workflow status changed during
  the cutover window) only while nobody has changed it in this system (``version = 1``).
- **All or nothing, verified.** One transaction, as the owner role under the clinic's row-level
  security (so every row written must belong to that clinic). Before commit every imported row is
  read back and compared field by field with the source; any difference rolls everything back.
- **No personal data in output.** It prints counts only.

Not imported, by design (data minimisation; this system never stores them): caller and patient
names, dates of birth, legacy patient ids, triage routes, free-text actor names and promise
subjects. The counts of such values left behind are reported. Priority flags are not computed here:
they are backfilled for every call, imported or live, when the classifier is ported.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

WORKFLOW = {"pending", "following_up", "addressed", "no_action_needed"}
PROMISE_STATUS = {
    "open": "open",
    "fulfilled": "fulfilled",
    "done": "fulfilled",
    "completed": "fulfilled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}
URGENT_CATEGORIES = {"urgent_doctor"}  # what the legacy urgent-alert tool filed messages under
MESSAGE_MAX = 4000  # messages.detail CHECK
NOTE_MAX = 2000  # call_interactions.note CHECK
COST_PLACES = Decimal("0.0001")  # calls.cost_usd numeric(10,4)


@dataclass
class Report:
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def lines(self) -> list[str]:
        return [f"{k}: {v}" for k, v in sorted(self.counts.items())]


@dataclass
class Plan:
    calls: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    promises: list[dict[str, Any]] = field(default_factory=list)
    interactions: list[dict[str, Any]] = field(default_factory=list)


class ImportRefused(Exception):
    """A refusal or a failed verification; nothing was committed."""

    def __init__(self, message: str, report: Report | None = None) -> None:
        super().__init__(message)
        self.report = report


class _DryRun(Exception):
    """Raised inside the transaction so a verified dry run rolls back."""


def _url(raw: str) -> str:
    """psycopg URL; TLS required for anything that isn't local or on the private network."""
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix) :]
    host = urlsplit(raw).hostname or ""
    local = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".railway.internal")
    if not local and "sslmode=" not in raw:
        raw += ("&" if "?" in raw else "?") + "sslmode=require"
    return raw


def _clip(value: str | None, limit: int, report: Report, key: str) -> str | None:
    if value is None or len(value) <= limit:
        return value
    report.add(key)
    return value[: limit - 1] + "…"


def _cost(value: Any, report: Report) -> Decimal | None:
    if value is None:
        return None
    try:
        cost = Decimal(repr(float(value))).quantize(COST_PLACES)
    except (InvalidOperation, ValueError):
        report.add("calls.invalid_cost_dropped")
        return None
    if cost < 0:
        report.add("calls.invalid_cost_dropped")
        return None
    return cost


# ---------------------------------------------------------------------------------------------
# Read (legacy)


_LEGACY_CALLS = """
    SELECT call_id, agent_id, practice, call_time, caller_phone, intent, call_summary, transcript,
           duration_seconds, call_cost, user_sentiment, created_at, workflow_status,
           patient_name, caller_name, caller_dob, patient_id
    FROM calls
    WHERE agent_id = ANY(%(agents)s) OR (agent_id IS NULL AND practice = ANY(%(practices)s))
    ORDER BY call_id
"""
_LEGACY_MESSAGES = """
    SELECT id, call_id, category, concern_type, detail, days_post_op, preferred_contact,
           callback_number, patient_id, created_at
    FROM w1_messages ORDER BY id
"""
_LEGACY_PROMISES = """
    SELECT id, call_id, promise_type, subject, due_at, status, created_at, fulfilled_at,
           fulfilled_by, patient_id
    FROM w1_promises ORDER BY id
"""
_LEGACY_INTERACTIONS = """
    SELECT id, call_id, action_type, status_from, status_to, note, actor_name, created_at
    FROM call_interactions ORDER BY id
"""


def read_legacy(legacy_url: str, agents: list[str], practices: list[str]) -> dict[str, list[Any]]:
    with psycopg.connect(_url(legacy_url), row_factory=dict_row) as conn:
        # One consistent snapshot of all four tables, and no way to write by accident.
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        data = {
            "calls": conn.execute(
                _LEGACY_CALLS, {"agents": agents, "practices": practices}
            ).fetchall(),
            "messages": conn.execute(_LEGACY_MESSAGES).fetchall(),
            "promises": conn.execute(_LEGACY_PROMISES).fetchall(),
            "interactions": conn.execute(_LEGACY_INTERACTIONS).fetchall(),
        }
        conn.rollback()
    return data


# ---------------------------------------------------------------------------------------------
# Transform


def _local(started: datetime | None, tz: ZoneInfo) -> tuple[Any, Any, Any]:
    if started is None:
        return None, None, None
    local = started.astimezone(tz)
    return local.date(), local.hour, local.weekday()  # Monday = 0, as voice-gateway stores it


def _call(row: dict[str, Any], tz: ZoneInfo, report: Report) -> dict[str, Any]:
    started: datetime | None = row["call_time"]
    duration = row["duration_seconds"]
    if duration is not None and duration < 0:
        report.add("calls.invalid_duration_dropped")
        duration = None
    status = row["workflow_status"] or "pending"
    if status not in WORKFLOW:
        report.add("calls.unknown_status_as_pending")
        status = "pending"
    for dropped in ("patient_name", "caller_name", "caller_dob", "patient_id"):
        if row[dropped] is not None:
            report.add(f"left_behind.calls.{dropped}")
    ended = started + timedelta(seconds=duration) if started and duration is not None else None
    local_date, local_hour, local_dow = _local(started, tz)
    return {
        "provider_call_id": row["call_id"],
        "from_number": row["caller_phone"],
        "started_at": started,
        "ended_at": ended,
        "duration_seconds": duration,
        "cost_usd": _cost(row["call_cost"], report),
        "summary": row["call_summary"],
        "transcript": row["transcript"],
        "sentiment": row["user_sentiment"],
        "intent": row["intent"],
        "local_date": local_date,
        "local_hour": local_hour,
        "local_dow": local_dow,
        "workflow_status": status,
        "analyzed_at": row["created_at"],  # legacy stored only analysed calls
    }


def _placeholder_call(call_id: str, first_seen: datetime, tz: ZoneInfo) -> dict[str, Any]:
    """A call whose webhook never arrived, so its messages still reach the inbox."""
    local_date, local_hour, local_dow = _local(first_seen, tz)
    return {
        "provider_call_id": call_id,
        "from_number": None,
        "started_at": first_seen,
        "ended_at": None,
        "duration_seconds": None,
        "cost_usd": None,
        "summary": None,
        "transcript": None,
        "sentiment": None,
        "intent": None,
        "local_date": local_date,
        "local_hour": local_hour,
        "local_dow": local_dow,
        "workflow_status": "pending",
        "analyzed_at": None,
    }


def _message(m: dict[str, Any], report: Report) -> dict[str, Any]:
    if m["patient_id"] is not None:
        report.add("left_behind.messages.patient_id")
    metadata = {
        k: m[k] for k in ("concern_type", "days_post_op", "preferred_contact") if m[k] is not None
    }
    return {
        "provider_call_id": m["call_id"],
        "category": m["category"],
        "detail": _clip(m["detail"], MESSAGE_MAX, report, "messages.detail_truncated"),
        "callback_number": m["callback_number"],
        "metadata": json.dumps(metadata) if metadata else None,
        "urgent": m["category"] in URGENT_CATEGORIES,
        "dedupe_key": f"legacy:w1_messages:{m['id']}",
        "created_at": m["created_at"],
    }


def _promise(p: dict[str, Any], report: Report) -> dict[str, Any]:
    status = PROMISE_STATUS.get((p["status"] or "open").lower())
    if status is None:
        report.add("promises.unknown_status_as_open")
        status = "open"
    for dropped in ("subject", "fulfilled_by", "patient_id"):
        if p[dropped] is not None:
            report.add(f"left_behind.promises.{dropped}")
    return {
        "provider_call_id": p["call_id"],
        "promise_type": p["promise_type"],
        "due_at": p["due_at"],
        "status": status,
        "fulfilled_at": p["fulfilled_at"],
        "dedupe_key": f"legacy:w1_promises:{p['id']}",
        "created_at": p["created_at"],
    }


def _interaction(i: dict[str, Any], report: Report) -> dict[str, Any]:
    if i["actor_name"] is not None:
        report.add("left_behind.interactions.actor_name")
    return {
        "provider_call_id": i["call_id"],
        "action_type": i["action_type"],
        "status_from": i["status_from"],
        "status_to": i["status_to"],
        "note": _clip(i["note"], NOTE_MAX, report, "interactions.note_truncated"),
        "idempotency_key": f"legacy:call_interactions:{i['id']}",
        "created_at": i["created_at"],
    }


def plan_import(
    legacy: dict[str, list[Any]], timezone: str, *, orphans: bool, report: Report
) -> Plan:
    tz = ZoneInfo(timezone)
    plan = Plan(calls=[_call(row, tz, report) for row in legacy["calls"]])
    known = {c["provider_call_id"] for c in plan.calls}
    placeholders: dict[str, datetime] = {}

    def belongs(row: dict[str, Any], kind: str) -> bool:
        call_id, created = row["call_id"], row["created_at"]
        if call_id in known:
            return True
        if orphans and call_id and created is not None:
            placeholders[call_id] = min(placeholders.get(call_id, created), created)
            return True
        report.add(f"{kind}.not_this_clinic_or_orphan_skipped")
        return False

    plan.messages = [_message(m, report) for m in legacy["messages"] if belongs(m, "messages")]
    plan.promises = [_promise(p, report) for p in legacy["promises"] if belongs(p, "promises")]
    # History only means something on a call we have.
    plan.interactions = [
        _interaction(i, report) for i in legacy["interactions"] if i["call_id"] in known
    ]
    report.add(
        "interactions.not_this_clinic_skipped", len(legacy["interactions"]) - len(plan.interactions)
    )
    for call_id, first_seen in sorted(placeholders.items()):
        plan.calls.append(_placeholder_call(call_id, first_seen, tz))
        report.add("calls.placeholder_for_orphans")
    return plan


# ---------------------------------------------------------------------------------------------
# Write and verify (this system)

_UPSERT_CALL = """
    INSERT INTO calls (
      clinic_id, provider_call_id, direction, from_number, started_at, ended_at, duration_seconds,
      cost_usd, summary, transcript, sentiment, intent, local_date, local_hour, local_dow,
      workflow_status, source, analyzed_at
    ) VALUES (
      %(clinic_id)s, %(provider_call_id)s, 'inbound', %(from_number)s, %(started_at)s,
      %(ended_at)s, %(duration_seconds)s, %(cost_usd)s, %(summary)s, %(transcript)s,
      %(sentiment)s, %(intent)s, %(local_date)s, %(local_hour)s, %(local_dow)s,
      %(workflow_status)s, 'import', %(analyzed_at)s
    )
    ON CONFLICT (clinic_id, provider_call_id) DO UPDATE SET
      from_number = EXCLUDED.from_number, started_at = EXCLUDED.started_at,
      ended_at = EXCLUDED.ended_at, duration_seconds = EXCLUDED.duration_seconds,
      cost_usd = EXCLUDED.cost_usd, summary = EXCLUDED.summary, transcript = EXCLUDED.transcript,
      sentiment = EXCLUDED.sentiment, intent = EXCLUDED.intent, local_date = EXCLUDED.local_date,
      local_hour = EXCLUDED.local_hour, local_dow = EXCLUDED.local_dow,
      workflow_status = EXCLUDED.workflow_status, analyzed_at = EXCLUDED.analyzed_at,
      updated_at = now()
    WHERE calls.source = 'import' AND calls.version = 1
    RETURNING (xmax = 0) AS inserted
"""
_INSERT_MESSAGE = """
    INSERT INTO messages (clinic_id, provider_call_id, category, detail, callback_number,
                          metadata, urgent, dedupe_key, created_at)
    VALUES (%(clinic_id)s, %(provider_call_id)s, %(category)s, %(detail)s, %(callback_number)s,
            CAST(%(metadata)s AS jsonb), %(urgent)s, %(dedupe_key)s, %(created_at)s)
    ON CONFLICT (dedupe_key) DO NOTHING
"""
_INSERT_PROMISE = """
    INSERT INTO promises (clinic_id, provider_call_id, promise_type, due_at, status,
                          fulfilled_at, dedupe_key, created_at)
    VALUES (%(clinic_id)s, %(provider_call_id)s, %(promise_type)s, %(due_at)s, %(status)s,
            %(fulfilled_at)s, %(dedupe_key)s, %(created_at)s)
    ON CONFLICT (dedupe_key) DO NOTHING
"""
_INSERT_INTERACTION = """
    INSERT INTO call_interactions (clinic_id, call_id, action_type, status_from, status_to, note,
                                   idempotency_key, created_at)
    SELECT %(clinic_id)s, c.id, %(action_type)s, %(status_from)s, %(status_to)s, %(note)s,
           %(idempotency_key)s, %(created_at)s
    FROM calls c WHERE c.clinic_id = %(clinic_id)s AND c.provider_call_id = %(provider_call_id)s
    ON CONFLICT (clinic_id, idempotency_key) DO NOTHING
"""


def write(conn: psycopg.Connection[Any], clinic_id: uuid.UUID, plan: Plan, report: Report) -> None:
    for call in plan.calls:
        row = conn.execute(_UPSERT_CALL, {**call, "clinic_id": clinic_id}).fetchone()
        if row is None:
            report.add("calls.kept_live_or_changed_here")
        else:
            report.add("calls.inserted" if row["inserted"] else "calls.refreshed")
    for table, statement, rows in (
        ("messages", _INSERT_MESSAGE, plan.messages),
        ("promises", _INSERT_PROMISE, plan.promises),
        ("interactions", _INSERT_INTERACTION, plan.interactions),
    ):
        for item in rows:
            written = conn.execute(statement, {**item, "clinic_id": clinic_id}).rowcount
            report.add(f"{table}.inserted" if written else f"{table}.already_present")


_STORED_CALLS = """
    SELECT provider_call_id, source, version, from_number, started_at, ended_at, duration_seconds,
           cost_usd, summary, transcript, sentiment, intent, local_date, local_hour, local_dow,
           workflow_status
    FROM calls WHERE clinic_id = %s AND provider_call_id = ANY(%s)
"""
_STORED_MESSAGES = """
    SELECT dedupe_key AS key, provider_call_id, category, detail, callback_number, urgent,
           created_at
    FROM messages WHERE clinic_id = %s AND dedupe_key = ANY(%s)
"""
_STORED_PROMISES = """
    SELECT dedupe_key AS key, provider_call_id, promise_type, due_at, status, fulfilled_at,
           created_at
    FROM promises WHERE clinic_id = %s AND dedupe_key = ANY(%s)
"""
_STORED_INTERACTIONS = """
    SELECT i.idempotency_key AS key, c.provider_call_id, i.action_type, i.status_from,
           i.status_to, i.note, i.created_at
    FROM call_interactions i JOIN calls c ON c.id = i.call_id
    WHERE i.clinic_id = %s AND i.idempotency_key = ANY(%s)
"""
_CALL_FIELDS = ("from_number", "started_at", "ended_at", "duration_seconds", "cost_usd",
                "summary", "transcript", "sentiment", "intent", "local_date", "local_hour",
                "local_dow")  # fmt: skip


def verify(conn: psycopg.Connection[Any], clinic_id: uuid.UUID, plan: Plan) -> list[str]:
    """Differences between the plan (from the source) and what the database now holds.
    Field names only: never the values, which are personal data."""
    problems: list[str] = []
    stored = {
        r["provider_call_id"]: r
        for r in conn.execute(
            _STORED_CALLS, (clinic_id, [c["provider_call_id"] for c in plan.calls])
        )
    }
    for call in plan.calls:
        row = stored.get(call["provider_call_id"])
        if row is None:
            problems.append("calls: missing")
        elif row["source"] == "import":  # a call ingested live is this system's own record
            names = _CALL_FIELDS + (("workflow_status",) if row["version"] == 1 else ())
            problems += [f"calls: {name} differs" for name in names if row[name] != call[name]]

    for table, statement, rows, key in (
        ("messages", _STORED_MESSAGES, plan.messages, "dedupe_key"),
        ("promises", _STORED_PROMISES, plan.promises, "dedupe_key"),
        ("call_interactions", _STORED_INTERACTIONS, plan.interactions, "idempotency_key"),
    ):
        found = {r["key"]: r for r in conn.execute(statement, (clinic_id, [r[key] for r in rows]))}
        for wanted in rows:
            row = found.get(wanted[key])
            if row is None:
                problems.append(f"{table}: missing")
                continue
            problems += [
                f"{table}: {name} differs"
                for name in row
                if name != "key" and name in wanted and row[name] != wanted[name]
            ]
    return problems


def run(
    legacy_url: str,
    admin_url: str,
    slug: str,
    agents: list[str],
    practices: list[str],
    *,
    orphans: bool,
    apply: bool,
) -> Report:
    if not agents and not practices:
        raise ImportRefused("name the clinic's legacy agent ids (WASSUP_IMPORT_AGENTS)")
    report = Report()
    legacy = read_legacy(legacy_url, agents, practices)
    report.add("source.calls", len(legacy["calls"]))
    with psycopg.connect(_url(admin_url), row_factory=dict_row) as conn:
        clinic = conn.execute(
            "SELECT id, timezone FROM clinics WHERE slug = %s", (slug,)
        ).fetchone()  # as admin: the clinic context isn't set yet
        if clinic is None:
            raise ImportRefused(f"no clinic with slug {slug!r}: create it first")
        clinic_id: uuid.UUID = clinic["id"]
        plan = plan_import(legacy, clinic["timezone"], orphans=orphans, report=report)
        try:
            with conn.transaction():
                conn.execute("SET LOCAL ROLE wassup_owner")
                conn.execute("SELECT set_config('app.clinic_ids', %s, true)", (f"{{{clinic_id}}}",))
                conn.execute("SET LOCAL lock_timeout = '5s'")
                conn.execute(  # one import per clinic at a time
                    "SELECT pg_advisory_xact_lock(hashtext('legacy-import:' || %s))",
                    (str(clinic_id),),
                )
                write(conn, clinic_id, plan, report)
                problems = verify(conn, clinic_id, plan)
                if problems:
                    for problem in sorted(set(problems)):
                        report.add(f"verify failed, {problem}", problems.count(problem))
                    raise ImportRefused("verification failed; nothing was committed", report)
                report.add("verified")
                conn.execute(
                    "INSERT INTO audit_log (clinic_id, actor_service, action, target_type,"
                    " target_id, detail) VALUES (%s, 'legacy-import', 'legacy.import', 'clinic',"
                    " %s, CAST(%s AS jsonb))",
                    (clinic_id, str(clinic_id), json.dumps(report.counts)),
                )
                if not apply:
                    raise _DryRun
        except _DryRun:
            pass
    return report


def _split(raw: str, sep: str) -> list[str]:
    return [x.strip() for x in raw.split(sep) if x.strip()]


def main() -> int:
    env = os.environ
    required = ("WASSUP_LEGACY_DATABASE_URL", "WASSUP_ADMIN_DATABASE_URL", "WASSUP_IMPORT_CLINIC")
    missing = [name for name in required if not env.get(name)]
    if missing:
        print(f"not set: {', '.join(missing)}", file=sys.stderr)
        return 2
    apply = env.get("WASSUP_IMPORT_APPLY") == "true"
    try:
        report = run(
            env["WASSUP_LEGACY_DATABASE_URL"],
            env["WASSUP_ADMIN_DATABASE_URL"],
            env["WASSUP_IMPORT_CLINIC"],
            _split(env.get("WASSUP_IMPORT_AGENTS", ""), ","),
            _split(env.get("WASSUP_IMPORT_PRACTICE_NAMES", ""), "|"),
            orphans=env.get("WASSUP_IMPORT_ORPHANS") == "true",
            apply=apply,
        )
    except ImportRefused as exc:
        if exc.report is not None:
            print("\n".join(exc.report.lines()), file=sys.stderr)
        print(f"import refused: {exc}", file=sys.stderr)
        return 1
    print("\n".join(report.lines()))
    print(
        "committed" if apply else "dry run: verified, then rolled back (WASSUP_IMPORT_APPLY=true)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
