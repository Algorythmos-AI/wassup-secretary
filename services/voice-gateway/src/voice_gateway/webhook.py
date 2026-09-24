"""POST /v1/retell/webhook — signed call events from Retell.

Order of operations (ADR 0004):
1. verify the signature on the raw bytes (401 otherwise);
2. store the raw event (a database outage here → 503, so Retell retries);
3. synthetic line-check calls stop here (never stored as patient calls) — only when a line check
   for that exact pair of our numbers is actually running (caller ID can be spoofed);
4. resolve the clinic from the signed agent AND the dialled number; mismatch → quarantine;
5. in ONE clinic-scoped transaction: upsert the call, enqueue the outbox event, mark processed.
A failure after step 2 is recorded on the raw event, and ops-worker replays it. A *transient*
database failure also answers 503, so Retell's own retry gets a second chance straight away.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    IntegrityError,
    ProgrammingError,
)
from sqlalchemy.exc import (
    TimeoutError as PoolTimeoutError,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import clinic_scope, unscoped
from wassup_core.http import problem
from wassup_core.logging import get_logger

from voice_gateway import store
from voice_gateway.retell import ANALYZED_EVENT, KNOWN_EVENTS, PayloadError, parse_event, to_record
from voice_gateway.settings import VoiceGatewaySettings
from voice_gateway.signature import verify

router = APIRouter()
log = get_logger(__name__)


def is_transient(exc: BaseException) -> bool:
    """Would the same request plausibly succeed in a moment? Connection loss, pool exhaustion,
    statement timeout: yes. A constraint violation or a bad statement: no (that is a bug, and
    retrying it only delays the replay job and the alert)."""
    if isinstance(exc, IntegrityError | ProgrammingError | DataError):
        return False
    return isinstance(exc, DBAPIError | PoolTimeoutError | OSError | TimeoutError)


def _unavailable() -> JSONResponse:
    response = problem(503, "Temporarily unavailable", "db_unavailable")
    response.headers["retry-after"] = "5"
    return response


@router.post("/v1/retell/webhook", status_code=204)
async def retell_webhook(request: Request) -> Response:  # noqa: PLR0911 — one exit per documented outcome
    settings: VoiceGatewaySettings = request.app.state.settings
    engine: AsyncEngine = request.app.state.engine
    raw = await request.body()

    if not verify(raw, request.headers.get("x-retell-signature"), settings.retell_keys):
        log.warning("webhook_rejected", reason="bad_signature")
        return problem(401, "Invalid signature", "invalid_signature")
    try:
        payload: Any = store.strip_nul(json.loads(raw))
        event, call = parse_event(payload)
        record = to_record(event, call)
    except (ValueError, PayloadError):
        return problem(400, "Invalid event", "invalid_event")

    if event not in KNOWN_EVENTS:
        return Response(status_code=204)

    try:
        async with unscoped(engine) as conn:
            raw_id = await store.store_raw(conn, event, record, payload)
            if await store.is_synthetic(conn, call, settings.ai_lines):
                if call.get("direction") != "outbound":  # the answered leg proves the line works
                    await store.record_canary_receipt(conn, record.to_number)
                await store.mark_raw(conn, raw_id, error=None)
                log.info("synthetic_call", call_id=record.provider_call_id, event_type=event)
                return Response(status_code=204)
            clinic_id = await store.resolve_clinic(conn, record.agent_id, record.to_number)
            if clinic_id is None:
                await store.quarantine(conn, "unknown_agent_or_number", record.agent_id, payload)
                await store.mark_raw(conn, raw_id, error="quarantined:unknown_agent_or_number")
                log.error(
                    "webhook_quarantined", call_id=record.provider_call_id, agent_id=record.agent_id
                )
                return Response(status_code=204)
    except Exception as exc:
        if not is_transient(exc):
            raise
        log.error("webhook_db_unavailable", code=type(exc).__name__)
        return _unavailable()

    try:
        async with clinic_scope(engine, [clinic_id]) as conn:
            timezone = await store.clinic_timezone(conn, clinic_id)
            call_uuid = await store.upsert_call(conn, clinic_id, record, timezone)
            if event == ANALYZED_EVENT:
                await store.enqueue(
                    conn,
                    clinic_id,
                    "call.analyzed",
                    f"call_analyzed:{record.provider_call_id}",
                    {"call_id": str(call_uuid)},
                )
            await store.mark_raw(conn, raw_id, clinic_id=clinic_id)
        log.info(
            "call_stored",
            call_id=record.provider_call_id,
            clinic_id=str(clinic_id),
            event_type=event,
        )
    except Exception as exc:  # replayable: the raw event is stored
        log.error(
            "webhook_processing_failed", call_id=record.provider_call_id, code=type(exc).__name__
        )
        try:
            async with unscoped(engine) as conn:
                await store.mark_raw(conn, raw_id, error=f"processing_failed:{type(exc).__name__}")
        except Exception as mark_exc:
            # The raw row stays open (processed_at NULL, no error): replay picks it up anyway.
            log.error(
                "webhook_mark_failed", call_id=record.provider_call_id, code=type(mark_exc).__name__
            )
        if is_transient(exc):
            return _unavailable()
    return Response(status_code=204)
