"""A clinic's active classification rules, cached briefly so the webhook path stays fast.

Rules change rarely (a new version is activated by an operator) and the webhook must not spend a
query per event on them, so each clinic's active rule set is cached for ``TTL_S`` seconds. A rule
set that fails validation is logged and treated as "no rules": the call is stored unclassified
rather than dropped; ``db/classifier_rules.py`` (``WASSUP_ROLE=reclassify``) classifies it later.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from wassup_core.classify import CallFacts, Classification, RuleSet, classify
from wassup_core.logging import get_logger

from voice_gateway.retell import CallRecord

log = get_logger(__name__)

TTL_S = 60.0

_ACTIVE = text(
    "SELECT version, rules FROM clinic_classifier_rules WHERE clinic_id = :c AND active LIMIT 1"
)


@dataclass(frozen=True)
class ActiveRules:
    version: int
    rules: RuleSet


@dataclass(frozen=True)
class ClassifiedCall:
    version: int
    result: Classification


class RulesCache:
    def __init__(self, ttl_s: float = TTL_S) -> None:
        self._ttl = ttl_s
        self._entries: dict[uuid.UUID, tuple[float, ActiveRules | None]] = {}

    async def active(self, conn: AsyncConnection, clinic_id: uuid.UUID) -> ActiveRules | None:
        now = time.monotonic()
        cached = self._entries.get(clinic_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        row = (await conn.execute(_ACTIVE, {"c": clinic_id})).first()
        active: ActiveRules | None = None
        if row is not None:
            try:
                active = ActiveRules(int(row.version), RuleSet.parse(row.rules))
            except (ValueError, TypeError):
                # Never let a bad rule set break ingestion; the call is stored unclassified.
                log.error("classifier_rules_invalid", clinic_id=str(clinic_id), version=row.version)
        self._entries[clinic_id] = (now + self._ttl, active)
        return active

    def forget(self, clinic_id: uuid.UUID | None = None) -> None:
        if clinic_id is None:
            self._entries.clear()
        else:
            self._entries.pop(clinic_id, None)


def facts(record: CallRecord) -> CallFacts:
    return CallFacts(
        summary=record.summary,
        transcript=record.transcript,
        intent=record.intent,
        sentiment=record.sentiment,
        triage_route=record.triage_route,
        call_successful=record.call_successful,
    )


async def classify_record(
    cache: RulesCache, conn: AsyncConnection, clinic_id: uuid.UUID, record: CallRecord
) -> ClassifiedCall | None:
    """The classification for an analysed call, or None when the call is not yet analysed or the
    clinic has no active rules (the call keeps whatever it had)."""
    if not record.analyzed:
        return None
    active = await cache.active(conn, clinic_id)
    if active is None:
        return None
    return ClassifiedCall(active.version, classify(facts(record), active.rules))
