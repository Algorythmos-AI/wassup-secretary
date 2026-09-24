"""Voice configuration drift: is every clinic number bound the way the database says it should be?

A clinic's calls are only captured if its number is bound to the clinic's agent, at a *published*
version, and that version posts to our webhook. Each of these has gone wrong before, silently:
numbers were once bound to a draft version (so any dashboard edit went live on the next call), and
a wrong webhook URL means calls happen but never reach the dashboard. This job compares the voice
provider's live configuration with ``clinic_phone_numbers`` / ``clinic_voice_agents`` and reports,
per number: ``ok``, ``not_found``, ``unbound``, ``wrong_agent``, ``floating_version`` (bound to
"latest", i.e. whatever draft is newest), ``wrong_version``, ``unpublished_version`` or
``webhook_mismatch``.

Numbers and agent ids are configuration, not personal data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import clinic_scope, unscoped
from wassup_core.logging import get_logger

from ops_worker.notifier import EmailSender
from ops_worker.watch import Watch

log = get_logger(__name__)

STALE_AFTER_S = 3 * 3600
REALERT_EVERY_S = 12 * 3600


class VoiceConfigApi(Protocol):
    async def get_phone_number(self, e164: str) -> dict[str, Any] | None: ...
    async def get_agent_version(self, agent_id: str, version: int) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class Expected:
    """What the database says about one number: the agents (and pinned versions) it may use."""

    e164: str
    agents: dict[str, int | None]


@dataclass
class VoiceConfigMonitor:
    environment: str
    webhook_url: str
    ops_emails: list[str]
    watch: Watch = field(default_factory=lambda: Watch(STALE_AFTER_S, REALERT_EVERY_S))

    def report(self, now: float | None = None) -> dict[str, Any]:
        return self.watch.report(now)


_EXPECTED = text(
    """
    SELECT n.e164, a.agent_id, a.agent_version
    FROM clinic_phone_numbers n
    JOIN clinic_voice_agents a ON a.clinic_id = n.clinic_id AND a.active AND a.environment = :env
    WHERE n.active
    ORDER BY n.e164
    """
)


async def expected_bindings(engine: AsyncEngine, environment: str) -> list[Expected]:
    async with unscoped(engine) as conn:
        clinics = (await conn.execute(text("SELECT active_clinic_ids()"))).scalar() or []
    if not clinics:
        return []
    async with clinic_scope(engine, clinics) as conn:
        rows = (await conn.execute(_EXPECTED, {"env": environment})).all()
    by_number: dict[str, dict[str, int | None]] = {}
    for row in rows:
        by_number.setdefault(row.e164, {})[row.agent_id] = row.agent_version
    return [Expected(e164, agents) for e164, agents in by_number.items()]


def _weight(agent: dict[str, Any]) -> float:
    weight = agent.get("weight")
    return 1.0 if weight is None else float(weight)


def bound_agents(number: dict[str, Any]) -> list[tuple[str, int | None]]:
    """(agent_id, version) pairs a number routes inbound calls to. Handles both the weighted
    ``inbound_agents`` list and the older single ``inbound_agent_id`` / ``_version`` fields."""
    agents = number.get("inbound_agents")
    if isinstance(agents, list) and agents:
        return [
            (str(a["agent_id"]), a.get("agent_version"))
            for a in agents
            if isinstance(a, dict) and a.get("agent_id") and _weight(a) > 0
        ]
    if number.get("inbound_agent_id"):
        return [(str(number["inbound_agent_id"]), number.get("inbound_agent_version"))]
    return []


def _agent_problem(
    expected: Expected,
    agent_id: str,
    version: int | None,
    versions: dict[str, dict[int, dict[str, Any]]],
    webhook_url: str,
) -> str | None:
    if agent_id not in expected.agents:
        return "wrong_agent"
    if version is None:
        return "floating_version"
    pinned = expected.agents[agent_id]
    if pinned is not None and version != pinned:
        return "wrong_version"
    detail = versions.get(agent_id, {}).get(int(version))
    if detail is None or not detail.get("is_published"):
        return "unpublished_version"
    if webhook_url and detail.get("webhook_url") != webhook_url:
        return "webhook_mismatch"
    return None


def assess_number(
    expected: Expected,
    number: dict[str, Any] | None,
    versions: dict[str, dict[int, dict[str, Any]]],
    webhook_url: str,
) -> str:
    """The first problem found for one number, or 'ok'."""
    if number is None:
        return "not_found"
    bound = bound_agents(number)
    if not bound:
        return "unbound"
    for agent_id, version in bound:
        problem = _agent_problem(expected, agent_id, version, versions, webhook_url)
        if problem:
            return problem
    return "ok"


async def check(api: VoiceConfigApi, expected: list[Expected], webhook_url: str) -> dict[str, Any]:
    numbers: dict[str, dict[str, Any] | None] = {}
    pairs: set[tuple[str, int]] = set()
    for item in expected:
        number = await api.get_phone_number(item.e164)
        numbers[item.e164] = number
        pairs.update((a, v) for a, v in bound_agents(number or {}) if isinstance(v, int))
    # Each bound (agent, version) is fetched exactly: published flag and webhook are per version.
    versions: dict[str, dict[int, dict[str, Any]]] = {}
    for agent_id, version in sorted(pairs):
        detail = await api.get_agent_version(agent_id, version)
        if detail is not None:
            versions.setdefault(agent_id, {})[version] = detail
    states = {e.e164: assess_number(e, numbers[e.e164], versions, webhook_url) for e in expected}
    failing = {n: s for n, s in states.items() if s != "ok"}
    return {
        "status": "failing" if failing else "ok",
        "numbers": states,
        "checked": len(states),
        "webhook_checked": bool(webhook_url),
    }


async def tick(
    monitor: VoiceConfigMonitor, engine: AsyncEngine, api: VoiceConfigApi, email: EmailSender
) -> str:
    expected = await expected_bindings(engine, monitor.environment)
    report = await check(api, expected, monitor.webhook_url)
    alert_due, recovered = monitor.watch.record(report)
    if report["status"] == "failing":
        problems = {n: s for n, s in report["numbers"].items() if s != "ok"}
        log.error("voice_config_drift", count=len(problems))
        if alert_due:
            await _alert(email, monitor.ops_emails, problems)
    elif recovered:
        log.info("voice_config_recovered")
    return str(report["status"])


async def _alert(email: EmailSender, ops_emails: list[str], problems: dict[str, str]) -> None:
    if not ops_emails:
        log.error("voice_config_drift_nobody_alerted", count=len(problems))
        return
    body = "\n".join(
        [
            "A clinic number's voice configuration no longer matches what WASSUP expects.",
            "Calls to it may be answered by the wrong agent, by an unreviewed draft, or not reach",
            "the dashboard at all.",
            "",
            *(f"{number}: {state}" for number, state in sorted(problems.items())),
            "",
            "Rebind the number to the clinic's published agent version",
            "(docs/runbooks/go-live.md, section 4).",
        ]
    )
    try:
        await email.send(ops_emails, "WASSUP voice configuration drift", body)
    except Exception as exc:
        log.error("voice_config_alert_failed", code=type(exc).__name__)
