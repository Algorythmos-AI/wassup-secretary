"""Replay, end to end: voice-gateway stores a request, fails to finish it, and ops-worker's replay
job recovers it through voice-gateway's own endpoint — exactly once."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from ops_worker import replay
from ops_worker.main import build_app as build_ops_app
from ops_worker.settings import OpsWorkerSettings
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from voice_gateway import store, tools
from voice_gateway.main import build_app as build_voice_app
from voice_gateway.settings import VoiceGatewaySettings
from voice_gateway.signature import verify
from wassup_core.db import make_engine
from wassup_core.settings import Environment

from tests.support.database import Seed

pytestmark = pytest.mark.db

KEY = "replay-test-key"
AGENT_A = "agent_test_a"
NUMBER_A = "+61400000101"


def _engine_as(db_url: str, role: str) -> AsyncEngine:
    engine = make_engine(db_url, pool_size=3)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_role(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute(f"SET ROLE {role}")
        cursor.close()

    return engine


@pytest.fixture
async def engines(db_url: str) -> AsyncIterator[tuple[AsyncEngine, AsyncEngine]]:
    voice, ops = _engine_as(db_url, "app_voice"), _engine_as(db_url, "app_ops")
    yield voice, ops
    await voice.dispose()
    await ops.dispose()


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str, str]] = []

    async def send(self, to: list[str], subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


def _voice_client(engine: AsyncEngine) -> httpx.AsyncClient:
    settings = VoiceGatewaySettings(environment=Environment.TEST, retell_api_key=KEY)
    transport = httpx.ASGITransport(app=build_voice_app(settings, engine=engine))
    return httpx.AsyncClient(transport=transport, base_url="http://voice-gateway")


def _gateway(client: httpx.AsyncClient) -> replay.Gateway:
    return replay.Gateway("http://voice-gateway", replay.retell_signer(KEY), client)


def _age(db_engine: Engine, table: str, column: str, value: str, minutes: int) -> None:
    """Pretend the item arrived ``minutes`` ago, so its replay backoff has passed."""
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text(
                f"UPDATE {table} SET received_at = now() - make_interval(mins => :m) "  # noqa: S608
                f"WHERE {column} = :v"
            ),
            {"m": minutes, "v": value},
        )


def _one(db_engine: Engine, sql: str, **params: Any) -> Any:
    with db_engine.connect() as conn:
        return conn.execute(text(sql), params).mappings().first()


def _attempts(db_engine: Engine, call_id: str) -> int:
    row = _one(
        db_engine,
        "SELECT replay_attempts FROM tool_requests_raw WHERE provider_call_id = :c",
        c=call_id,
    )
    return int(row["replay_attempts"])


def test_ops_signer_matches_the_gateway_verifier() -> None:
    body = b'{"event":"call_analyzed"}'
    now = int(time.time() * 1000)
    assert verify(body, replay.retell_signer(KEY)(body, now), [KEY], now_ms=now)


async def test_timed_out_urgent_message_is_recovered_by_replay(
    engines: tuple[AsyncEngine, AsyncEngine],
    db_engine: Engine,
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice, ops = engines
    call_id = f"call_{uuid.uuid4().hex}"
    spec = tools.TOOLS["capture_message"]

    async def stalls(ctx: tools.ToolContext, args: Any) -> dict[str, Any]:
        await spec.handler(ctx, args)  # writes, then the database "hangs" past the budget
        await asyncio.sleep(5)
        return {"ok": True}

    monkeypatch.setitem(tools.TOOLS, "capture_message", dataclasses.replace(spec, handler=stalls))
    envelope = {
        "call": {
            "call_id": call_id,
            "agent_id": AGENT_A,
            "direction": "inbound",
            "from_number": "+61400000555",
            "to_number": NUMBER_A,
        },
        "args": {"detail": "Wound is bleeding through the dressing", "urgent": True},
    }
    async with _voice_client(voice) as client:
        body = json.dumps(envelope).encode()
        response = await client.post(
            "/v1/retell/tools/test-clinic-a/capture_message",
            content=body,
            headers={
                "x-retell-signature": replay.retell_signer(KEY)(body, int(time.time() * 1000))
            },
        )
        # The caller is told the truth: it was NOT saved.
        assert response.json() == {"ok": False, "degraded": True}
        assert (
            _one(db_engine, "SELECT id FROM messages WHERE provider_call_id = :c", c=call_id)
            is None
        )
        raw = _one(
            db_engine,
            "SELECT completed_at, payload FROM tool_requests_raw WHERE provider_call_id = :c",
            c=call_id,
        )
        assert raw["completed_at"] is None and raw["payload"]["args"]["urgent"] is True

        monkeypatch.setitem(tools.TOOLS, "capture_message", spec)  # the database recovers
        await replay.run(ops, _gateway(client), FakeSender(), [])
        assert _attempts(db_engine, call_id) == 0  # too soon: the backoff hasn't passed
        _age(db_engine, "tool_requests_raw", "provider_call_id", call_id, minutes=2)
        await replay.run(ops, _gateway(client), FakeSender(), [])
        assert _attempts(db_engine, call_id) == 1
        _age(db_engine, "tool_requests_raw", "provider_call_id", call_id, minutes=60)
        await replay.run(ops, _gateway(client), FakeSender(), [])
        assert _attempts(db_engine, call_id) == 1  # completed: never replayed again

    message = _one(
        db_engine, "SELECT dedupe_key FROM messages WHERE provider_call_id = :c", c=call_id
    )
    assert message is not None
    urgent = _one(
        db_engine,
        "SELECT count(*) AS n FROM outbox_events WHERE dedupe_key = :k",
        k=f"message.urgent:{message['dedupe_key']}",
    )
    assert urgent["n"] == 1  # the clinic's urgent alert is now on its way
    raw = _one(
        db_engine,
        "SELECT outcome, clinic_id FROM tool_requests_raw WHERE provider_call_id = :c",
        c=call_id,
    )
    assert (raw["outcome"], raw["clinic_id"]) == ("done", seed.clinic_a)


async def test_webhook_transient_failure_is_503_then_replayed(
    engines: tuple[AsyncEngine, AsyncEngine],
    db_engine: Engine,
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice, ops = engines
    call_id = f"call_{uuid.uuid4().hex}"
    real_upsert = store.upsert_call

    async def db_blip(*_args: Any, **_kwargs: Any) -> uuid.UUID:
        raise OperationalError("upsert", {}, ConnectionResetError("connection reset"))

    monkeypatch.setattr(store, "upsert_call", db_blip)
    payload = {
        "event": "call_analyzed",
        "call": {
            "call_id": call_id,
            "agent_id": AGENT_A,
            "direction": "inbound",
            "from_number": "+61400000555",
            "to_number": NUMBER_A,
            "start_timestamp": int(time.time() * 1000) - 60_000,
        },
    }
    async with _voice_client(voice) as client:
        body = json.dumps(payload).encode()
        response = await client.post(
            "/v1/retell/webhook",
            content=body,
            headers={
                "x-retell-signature": replay.retell_signer(KEY)(body, int(time.time() * 1000))
            },
        )
        assert response.status_code == 503  # Retell retries too
        raw = _one(
            db_engine, "SELECT error FROM retell_events_raw WHERE provider_call_id = :c", c=call_id
        )
        assert raw["error"] == "processing_failed:OperationalError"

        monkeypatch.setattr(store, "upsert_call", real_upsert)
        _age(db_engine, "retell_events_raw", "provider_call_id", call_id, minutes=2)
        await replay.run(ops, _gateway(client), FakeSender(), [])

    stored = _one(db_engine, "SELECT clinic_id FROM calls WHERE provider_call_id = :c", c=call_id)
    assert stored["clinic_id"] == seed.clinic_a
    raw = _one(
        db_engine,
        "SELECT processed_at, error FROM retell_events_raw WHERE provider_call_id = :c",
        c=call_id,
    )
    assert raw["processed_at"] is not None and raw["error"] is None


async def test_webhook_bug_is_acknowledged_and_left_for_replay(
    engines: tuple[AsyncEngine, AsyncEngine], db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-transient failure (a bug): Retell retrying immediately won't help, so it gets 204;
    the raw event stays open for the replay job (which will retry after the fix, or alert)."""
    voice, _ = engines
    call_id = f"call_{uuid.uuid4().hex}"

    async def bug(*_args: Any, **_kwargs: Any) -> uuid.UUID:
        raise KeyError("bug")

    monkeypatch.setattr(store, "upsert_call", bug)
    payload = {
        "event": "call_ended",
        "call": {"call_id": call_id, "agent_id": AGENT_A, "to_number": NUMBER_A},
    }
    async with _voice_client(voice) as client:
        body = json.dumps(payload).encode()
        response = await client.post(
            "/v1/retell/webhook",
            content=body,
            headers={
                "x-retell-signature": replay.retell_signer(KEY)(body, int(time.time() * 1000))
            },
        )
    assert response.status_code == 204
    raw = _one(
        db_engine, "SELECT error FROM retell_events_raw WHERE provider_call_id = :c", c=call_id
    )
    assert raw["error"] == "processing_failed:KeyError"


async def test_exhausted_replays_alert_ops_and_turn_health_red(
    engines: tuple[AsyncEngine, AsyncEngine], db_engine: Engine
) -> None:
    _, ops = engines
    call_id = f"call_{uuid.uuid4().hex}"
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text(
                "INSERT INTO tool_requests_raw (dedupe_key, tool, clinic_slug, provider_call_id, payload, "
                "replay_attempts, received_at) VALUES (:k, 'capture_message', 'test-clinic-a', :c, "
                "'{}'::jsonb, :n, now() - interval '3 hours')"
            ),
            {"k": f"tool:{uuid.uuid4().hex}", "c": call_id, "n": replay.MAX_REPLAYS - 1},
        )

    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gateway down", request=request)

    sender = FakeSender()
    async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as client:
        result = await replay.run(ops, _gateway(client), sender, ["ops@example.test"])
    assert result["exhausted"] >= 1
    [(to, _subject, body)] = [s for s in sender.sent if "replay exhausted" in s[1]]
    assert to == ["ops@example.test"]
    assert "replay_unreachable:ConnectError" in body

    settings = OpsWorkerSettings(environment=Environment.TEST, scheduler_enabled=False)
    app = build_ops_app(settings, engine=ops, retell=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ops"
    ) as client:
        health = await client.get("/health/replay")
    assert health.status_code == 503
    assert health.json()["tools_exhausted"] >= 1
    # Resolve it (the runbook's "abandon" step) so other tests see a clean state.
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text(
                "UPDATE tool_requests_raw SET completed_at = now(), outcome = 'abandoned' "
                "WHERE provider_call_id = :c"
            ),
            {"c": call_id},
        )
