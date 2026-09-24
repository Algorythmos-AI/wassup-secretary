"""Replay of raw webhook events and tool requests that voice-gateway stored but didn't finish.

voice-gateway commits every signed webhook event (``retell_events_raw``) and every write-tool
request (``tool_requests_raw``) *before* processing it. When processing then fails — the database
stumbled, the tool ran out of time, a bug — the raw row stays open. This job re-submits it to
voice-gateway exactly as Retell would (same JSON, fresh signature), so it goes through the same
code path, and voice-gateway's idempotency (upsert precedence for calls, the tool dedupe key for
tools) makes a replay of something that did in fact complete a harmless no-op.

- Backoff: attempt *n* waits ``REPLAY_AFTER * 2**n`` from receipt (1, 2, 4, 8, 16 minutes).
- After ``MAX_REPLAYS`` the item is exhausted: ops is emailed (ids only), and ``/health/replay``
  stays red until an operator resolves it (see docs/runbooks/replay-exhausted.md).
- Quarantined events (unknown agent or number) are configuration problems and are never replayed.
- Lookups are never stored raw (names and dates of birth are not kept for replay; a lookup after
  the call has ended is pointless anyway).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import unscoped
from wassup_core.logging import get_logger

from ops_worker.notifier import EmailSender

log = get_logger(__name__)

REPLAY_AFTER = timedelta(seconds=60)
MAX_REPLAYS = 5
BATCH = 20
# An open item older than this means replay itself isn't running (or can't reach the gateway).
STUCK_AFTER = timedelta(minutes=45)

_CLAIM_EVENTS = text(
    """
    UPDATE retell_events_raw SET replay_attempts = replay_attempts + 1
    WHERE id IN (
      SELECT id FROM retell_events_raw
      WHERE processed_at IS NULL
        AND (error IS NULL OR error LIKE 'processing_failed:%')
        AND replay_attempts < :max
        AND received_at < now() - make_interval(secs => :after * power(2, replay_attempts))
      ORDER BY id
      FOR UPDATE SKIP LOCKED
      LIMIT :n
    )
    RETURNING id, event, payload, replay_attempts
    """
)
_CLAIM_TOOLS = text(
    """
    UPDATE tool_requests_raw SET replay_attempts = replay_attempts + 1
    WHERE id IN (
      SELECT id FROM tool_requests_raw
      WHERE completed_at IS NULL
        AND replay_attempts < :max
        AND received_at < now() - make_interval(secs => :after * power(2, replay_attempts))
      ORDER BY id
      FOR UPDATE SKIP LOCKED
      LIMIT :n
    )
    RETURNING id, tool, clinic_slug, payload, replay_attempts
    """
)
_EVENT_FAILED = text("UPDATE retell_events_raw SET error = :error WHERE id = :id")
_TOOL_FAILED = text("UPDATE tool_requests_raw SET last_error = :error WHERE id = :id")


class Signer(Protocol):
    def __call__(self, body: bytes, timestamp_ms: int) -> str: ...


def retell_signer(key: str) -> Signer:
    """Sign exactly as Retell does (``v=<ms>,d=<hex HMAC-SHA256(body + ms)>``). A contract test
    checks this against voice-gateway's verifier."""

    def sign(body: bytes, timestamp_ms: int) -> str:
        mac = hmac.new(key.encode(), body + str(timestamp_ms).encode(), hashlib.sha256)
        return f"v={timestamp_ms},d={mac.hexdigest()}"

    return sign


@dataclass(frozen=True)
class Gateway:
    """Where and how to re-submit: voice-gateway's base URL and the Retell signing key."""

    base_url: str
    sign: Signer
    client: httpx.AsyncClient


async def _post(gateway: Gateway, path: str, payload: dict[str, Any]) -> httpx.Response:
    body = json.dumps(payload).encode()
    signature = gateway.sign(body, int(time.time() * 1000))
    return await gateway.client.post(
        gateway.base_url.rstrip("/") + path,
        content=body,
        headers={"content-type": "application/json", "x-retell-signature": signature},
    )


async def _replay_event(gateway: Gateway, row: Any) -> str | None:
    """Returns an error code, or None when voice-gateway accepted the event."""
    try:
        response = await _post(gateway, "/v1/retell/webhook", row.payload)
    except httpx.HTTPError as exc:
        return f"replay_unreachable:{type(exc).__name__}"
    return None if response.status_code == 204 else f"replay_http_{response.status_code}"


async def _replay_tool(gateway: Gateway, row: Any) -> str | None:
    try:
        response = await _post(
            gateway, f"/v1/retell/tools/{row.clinic_slug}/{row.tool}", row.payload
        )
    except httpx.HTTPError as exc:
        return f"replay_unreachable:{type(exc).__name__}"
    if response.status_code != 200:
        return f"replay_http_{response.status_code}"
    try:
        result = response.json()
    except ValueError:
        return "replay_bad_response"
    if not isinstance(result, dict) or result.get("degraded") or result.get("ok") is False:
        return "replay_degraded"
    return None


async def run(
    engine: AsyncEngine, gateway: Gateway, email: EmailSender, ops_emails: list[str]
) -> dict[str, int]:
    """One replay pass. Returns counts (for logs and tests)."""
    params = {"max": MAX_REPLAYS, "after": REPLAY_AFTER.total_seconds(), "n": BATCH}
    async with unscoped(engine) as conn:
        events = (await conn.execute(_CLAIM_EVENTS, params)).all()
        tools = (await conn.execute(_CLAIM_TOOLS, params)).all()

    exhausted: list[str] = []
    replayed = failed = 0
    for kind, rows, replay, mark in (
        ("webhook_event", events, _replay_event, _EVENT_FAILED),
        ("tool_request", tools, _replay_tool, _TOOL_FAILED),
    ):
        for row in rows:
            error = await replay(gateway, row)
            if error is None:
                replayed += 1
                log.info("replay_ok", job=kind, event_id=row.id, attempt=row.replay_attempts)
                continue
            failed += 1
            log.warning("replay_failed", job=kind, event_id=row.id, reason=error)
            async with unscoped(engine) as conn:
                await conn.execute(mark, {"id": row.id, "error": error[:200]})
            if row.replay_attempts >= MAX_REPLAYS:
                exhausted.append(f"{kind} {row.id} ({error})")

    if exhausted:
        await _alert_exhausted(email, ops_emails, exhausted)
    return {"replayed": replayed, "failed": failed, "exhausted": len(exhausted)}


async def _alert_exhausted(email: EmailSender, ops_emails: list[str], items: list[str]) -> None:
    log.error("replay_exhausted", count=len(items))
    if not ops_emails:
        log.error("replay_exhausted_nobody_alerted", count=len(items))
        return
    body = "\n".join(
        [
            f"{len(items)} item(s) could not be processed after {MAX_REPLAYS} replays.",
            "An urgent message or a call record may be missing from the dashboard.",
            "",
            *items,
            "",
            "Runbook: docs/runbooks/replay-exhausted.md",
        ]
    )
    try:
        await email.send(ops_emails, "WASSUP replay exhausted", body)
    except Exception as exc:
        log.error("replay_alert_failed", code=type(exc).__name__)


_HEALTH = text(
    """
    SELECT
      (SELECT count(*) FROM retell_events_raw
        WHERE processed_at IS NULL AND replay_attempts >= :max
          AND (error IS NULL OR error NOT LIKE 'quarantined:%')) AS events_exhausted,
      (SELECT count(*) FROM retell_events_raw
        WHERE processed_at IS NULL AND (error IS NULL OR error NOT LIKE 'quarantined:%')
          AND received_at < now() - make_interval(secs => :stuck)) AS events_stuck,
      (SELECT count(*) FROM tool_requests_raw
        WHERE completed_at IS NULL AND replay_attempts >= :max) AS tools_exhausted,
      (SELECT count(*) FROM tool_requests_raw
        WHERE completed_at IS NULL
          AND received_at < now() - make_interval(secs => :stuck)) AS tools_stuck,
      (SELECT count(*) FROM tool_requests_raw WHERE completed_at IS NULL) AS tools_open
    """
)
_FAILING_KEYS = ("events_exhausted", "events_stuck", "tools_exhausted", "tools_stuck")


async def health(engine: AsyncEngine) -> dict[str, Any]:
    """Counts only. 'failing' when anything is exhausted or has been open too long."""
    params = {"max": MAX_REPLAYS, "stuck": STUCK_AFTER.total_seconds()}
    async with unscoped(engine) as conn:
        row = (await conn.execute(_HEALTH, params)).mappings().one()
    counts = {key: int(value) for key, value in row.items()}
    failing = any(counts[k] for k in _FAILING_KEYS)
    return {**counts, "status": "failing" if failing else "ok"}
