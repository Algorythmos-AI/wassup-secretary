"""Webhook ingestion against the migrated database, with the service connected as app_voice."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from voice_gateway.main import build_app
from voice_gateway.settings import VoiceGatewaySettings
from voice_gateway.signature import sign
from wassup_core.db import make_engine
from wassup_core.settings import Environment

from tests.support.database import Seed, line_check_running

pytestmark = pytest.mark.db

KEY = "test-signing-key"
CLINIC_A_AGENT = "agent_test_a"
CLINIC_A_NUMBER = "+61400000101"
CLINIC_B_NUMBER = "+61400000102"
AI_LINES = "+61400000900,+61400000901"
# 07:33 on Tuesday 22 Sep 2026 in Sydney (AEST, UTC+10).
SYDNEY_0733_MS = int(
    datetime(2026, 9, 22, 7, 33, tzinfo=ZoneInfo("Australia/Sydney")).timestamp() * 1000
)


@pytest.fixture
async def voice_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url, pool_size=2)

    @event.listens_for(engine.sync_engine, "connect")
    def _as_app_voice(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("SET ROLE app_voice")
        cursor.close()

    yield engine
    await engine.dispose()


@pytest.fixture
async def client(voice_engine: AsyncEngine, seed: Seed) -> AsyncIterator[httpx.AsyncClient]:
    settings = VoiceGatewaySettings(
        environment=Environment.TEST, retell_api_key=KEY, ai_line_numbers=AI_LINES
    )
    app = build_app(settings, engine=voice_engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _call(call_id: str, **overrides: Any) -> dict[str, Any]:
    start = SYDNEY_0733_MS
    call: dict[str, Any] = {
        "call_id": call_id,
        "agent_id": CLINIC_A_AGENT,
        "direction": "inbound",
        "from_number": "+61400000555",
        "to_number": CLINIC_A_NUMBER,
        "start_timestamp": start,
        "end_timestamp": start + 95_000,
        "duration_ms": 95_000,
        "disconnection_reason": "user_hangup",
        "call_cost": {"combined_cost": 12.5},
        "transcript": "Agent: Hello. User: Test caller wants a callback.",
        "call_analysis": {
            "call_summary": "Synthetic test caller asked for a callback.",
            "user_sentiment": "Neutral",
            "custom_analysis_data": {"intent": "callback"},
        },
    }
    call.update(overrides)
    return call


async def _post(
    client: httpx.AsyncClient,
    event_name: str,
    call: dict[str, Any],
    *,
    key: str = KEY,
    ts: int | None = None,
) -> httpx.Response:
    body = json.dumps({"event": event_name, "call": call}).encode()
    header = sign(body, key, ts if ts is not None else int(time.time() * 1000))
    return await client.post(
        "/v1/retell/webhook",
        content=body,
        headers={"x-retell-signature": header, "content-type": "application/json"},
    )


def _rows(db_engine: Engine, sql: str, **params: Any) -> list[Any]:
    with db_engine.connect() as conn:  # superuser: reads everything for assertions
        return list(conn.execute(text(sql), params).mappings())


async def test_analyzed_call_is_stored_for_the_right_clinic(
    client: httpx.AsyncClient, db_engine: Engine, seed: Seed
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    response = await _post(client, "call_analyzed", _call(call_id))
    assert response.status_code == 204
    [call] = _rows(db_engine, "SELECT * FROM calls WHERE provider_call_id = :c", c=call_id)
    assert call["clinic_id"] == seed.clinic_a
    assert str(call["cost_usd"]) == "0.1250"  # 12.5 cents → dollars
    assert call["summary"] == "Synthetic test caller asked for a callback."
    assert call["intent"] == "callback"
    assert call["analyzed_at"] is not None
    assert (call["local_hour"], call["local_dow"]) == (7, 1)  # 07:xx Tuesday in Sydney
    [outbox] = _rows(
        db_engine, "SELECT * FROM outbox_events WHERE dedupe_key = :k", k=f"call_analyzed:{call_id}"
    )
    assert outbox["payload"] == {"call_id": str(call["id"])}  # ids only
    [raw] = _rows(
        db_engine, "SELECT * FROM retell_events_raw WHERE provider_call_id = :c", c=call_id
    )
    assert raw["processed_at"] is not None and raw["clinic_id"] == seed.clinic_a


async def test_event_sequence_never_erases_earlier_facts(
    client: httpx.AsyncClient, db_engine: Engine
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    analyzed = _call(call_id)
    started = {
        k: v
        for k, v in analyzed.items()
        if k not in ("end_timestamp", "duration_ms", "call_analysis", "transcript")
    }
    for name, payload in (
        ("call_started", started),
        ("call_analyzed", analyzed),
        ("call_ended", _call(call_id, call_analysis={})),
    ):
        assert (await _post(client, name, payload)).status_code == 204
    [call] = _rows(db_engine, "SELECT * FROM calls WHERE provider_call_id = :c", c=call_id)
    # A late call_ended must not wipe the analysis written by call_analyzed.
    assert call["summary"] == "Synthetic test caller asked for a callback."
    assert call["duration_seconds"] == 95


async def test_duplicate_delivery_is_idempotent(
    client: httpx.AsyncClient, db_engine: Engine
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    for _ in range(3):
        assert (await _post(client, "call_analyzed", _call(call_id))).status_code == 204
    assert len(_rows(db_engine, "SELECT id FROM calls WHERE provider_call_id = :c", c=call_id)) == 1
    assert (
        len(
            _rows(
                db_engine,
                "SELECT id FROM outbox_events WHERE dedupe_key = :k",
                k=f"call_analyzed:{call_id}",
            )
        )
        == 1
    )
    assert (
        len(
            _rows(
                db_engine, "SELECT id FROM retell_events_raw WHERE provider_call_id = :c", c=call_id
            )
        )
        == 1
    )


async def test_unknown_agent_is_quarantined_not_stored(
    client: httpx.AsyncClient, db_engine: Engine
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    response = await _post(client, "call_analyzed", _call(call_id, agent_id="agent_nobody"))
    assert response.status_code == 204  # acknowledged so the provider stops retrying
    assert _rows(db_engine, "SELECT id FROM calls WHERE provider_call_id = :c", c=call_id) == []
    assert _rows(
        db_engine,
        "SELECT id FROM quarantine_events WHERE payload->'call'->>'call_id' = :c",
        c=call_id,
    )


async def test_agent_answering_another_clinics_number_is_quarantined(
    client: httpx.AsyncClient, db_engine: Engine
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    await _post(client, "call_analyzed", _call(call_id, to_number=CLINIC_B_NUMBER))
    assert _rows(db_engine, "SELECT id FROM calls WHERE provider_call_id = :c", c=call_id) == []
    [raw] = _rows(
        db_engine, "SELECT error FROM retell_events_raw WHERE provider_call_id = :c", c=call_id
    )
    assert raw["error"].startswith("quarantined")


async def test_bad_signature_is_rejected_and_nothing_stored(
    client: httpx.AsyncClient, db_engine: Engine
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    response = await _post(client, "call_analyzed", _call(call_id), key="attacker-key")
    assert response.status_code == 401
    assert (
        _rows(db_engine, "SELECT id FROM retell_events_raw WHERE provider_call_id = :c", c=call_id)
        == []
    )


async def test_stale_signature_is_rejected(client: httpx.AsyncClient) -> None:
    ten_minutes_ago = int(time.time() * 1000) - 600_000
    response = await _post(
        client, "call_analyzed", _call(f"call_{uuid.uuid4().hex}"), ts=ten_minutes_ago
    )
    assert response.status_code == 401


async def test_synthetic_line_check_is_never_a_patient_call(
    client: httpx.AsyncClient, db_engine: Engine
) -> None:
    inbound_leg = f"call_{uuid.uuid4().hex}"
    outbound_leg = f"call_{uuid.uuid4().hex}"
    line_check_running(db_engine, "+61400000900", CLINIC_A_NUMBER)
    await _post(client, "call_analyzed", _call(inbound_leg, from_number="+61400000900"))
    await _post(client, "call_analyzed", _call(outbound_leg, direction="outbound"))
    for call_id in (inbound_leg, outbound_leg):
        assert _rows(db_engine, "SELECT id FROM calls WHERE provider_call_id = :c", c=call_id) == []
        [raw] = _rows(
            db_engine,
            "SELECT processed_at FROM retell_events_raw WHERE provider_call_id = :c",
            c=call_id,
        )
        assert raw["processed_at"] is not None


async def test_spoofed_ai_line_caller_id_is_still_a_patient_call(
    client: httpx.AsyncClient, db_engine: Engine
) -> None:
    """Caller ID showing one of our AI lines, with no line check running for that pair, is a real
    caller (or someone spoofing): it must be stored, never silently dropped as synthetic."""
    call_id = f"call_{uuid.uuid4().hex}"
    await _post(
        client,
        "call_analyzed",
        _call(
            call_id, from_number="+61400000901", agent_id="agent_test_b", to_number=CLINIC_B_NUMBER
        ),
    )
    assert _rows(db_engine, "SELECT id FROM calls WHERE provider_call_id = :c", c=call_id)


async def test_invalid_json_is_400(client: httpx.AsyncClient) -> None:
    body = b"not json"
    response = await client.post(
        "/v1/retell/webhook",
        content=body,
        headers={"x-retell-signature": sign(body, KEY, int(time.time() * 1000))},
    )
    assert response.status_code == 400
