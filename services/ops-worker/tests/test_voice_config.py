"""Voice configuration drift monitor."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from ops_worker import voice_config
from ops_worker.main import build_app
from ops_worker.retell_api import RetellClient
from ops_worker.settings import OpsWorkerSettings
from ops_worker.voice_config import Expected, VoiceConfigMonitor
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import make_engine
from wassup_core.settings import Environment

from tests.support.database import Seed, as_role

HOOK = "https://voice.example.test/v1/retell/webhook"
EXPECTED = Expected("+61400000101", {"agent_test_a": 3})
VERSIONS = {
    "agent_test_a": {
        2: {"version": 2, "is_published": True, "webhook_url": HOOK},
        3: {"version": 3, "is_published": True, "webhook_url": HOOK},
        4: {"version": 4, "is_published": False, "webhook_url": HOOK},
    }
}


def _number(agent: str = "agent_test_a", version: int | None = 3, **extra: Any) -> dict[str, Any]:
    return {
        "phone_number": EXPECTED.e164,
        "inbound_agents": [{"agent_id": agent, "agent_version": version, "weight": 1}],
        **extra,
    }


@pytest.mark.parametrize(
    ("number", "versions", "state"),
    [
        (_number(), VERSIONS, "ok"),
        (None, VERSIONS, "not_found"),
        ({"phone_number": EXPECTED.e164, "inbound_agents": []}, VERSIONS, "unbound"),
        (_number(agent="agent_other"), VERSIONS, "wrong_agent"),
        (_number(version=None), VERSIONS, "floating_version"),  # the old "version 0 draft" risk
        (_number(version=2), VERSIONS, "wrong_version"),
        (
            _number(version=4),
            VERSIONS,
            "wrong_version",
        ),  # pinned 3 wins over "unpublished"
        (
            _number(version=3),
            {"agent_test_a": {3: {"version": 3, "is_published": False, "webhook_url": HOOK}}},
            "unpublished_version",
        ),
        (  # a version with no webhook at all can't be posting to ours
            _number(version=3),
            {"agent_test_a": {3: {"version": 3, "is_published": True}}},
            "webhook_mismatch",
        ),
        (
            _number(),
            {
                "agent_test_a": {
                    3: {"version": 3, "is_published": True, "webhook_url": "https://old"}
                }
            },
            "webhook_mismatch",
        ),
        (  # the older single-agent fields are understood too
            {"inbound_agent_id": "agent_test_a", "inbound_agent_version": 3},
            VERSIONS,
            "ok",
        ),
    ],
)
def test_assess_number(
    number: dict[str, Any] | None, versions: dict[str, dict[int, dict[str, Any]]], state: str
) -> None:
    assert voice_config.assess_number(EXPECTED, number, versions, HOOK) == state


def test_unpinned_agent_only_needs_a_published_version() -> None:
    unpinned = Expected(EXPECTED.e164, {"agent_test_a": None})
    assert voice_config.assess_number(unpinned, _number(version=2), VERSIONS, HOOK) == "ok"
    assert (
        voice_config.assess_number(unpinned, _number(version=4), VERSIONS, HOOK)
        == "unpublished_version"
    )


def test_zero_weight_agents_are_not_routed_to() -> None:
    number = _number()
    number["inbound_agents"].append({"agent_id": "agent_other", "agent_version": 1, "weight": 0})
    assert voice_config.bound_agents(number) == [("agent_test_a", 3)]


class FakeApi:
    def __init__(self, numbers: dict[str, dict[str, Any] | None]) -> None:
        self.numbers = numbers

    async def get_phone_number(self, e164: str) -> dict[str, Any] | None:
        return self.numbers.get(e164)

    async def get_agent_version(self, agent_id: str, version: int) -> dict[str, Any] | None:
        return VERSIONS.get(agent_id, {}).get(version)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str, str]] = []

    async def send(self, to: list[str], subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


@pytest.fixture
async def ops_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url, pool_size=2)

    @event.listens_for(engine.sync_engine, "connect")
    def _as_app_ops(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("SET ROLE app_ops")
        cursor.close()

    yield engine
    await engine.dispose()


@pytest.mark.db
async def test_tick_reads_expected_bindings_and_alerts_on_drift(
    ops_engine: AsyncEngine, db_engine: Engine, seed: Seed
) -> None:
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "wassup_owner", [seed.clinic_a])
        conn.execute(
            text("UPDATE clinic_voice_agents SET agent_version = 3 WHERE agent_id = 'agent_test_a'")
        )
    expected = await voice_config.expected_bindings(ops_engine, "staging")
    assert Expected("+61400000101", {"agent_test_a": 3}) in expected
    assert Expected("+61400000102", {"agent_test_b": None}) in expected

    monitor = VoiceConfigMonitor("staging", HOOK, ["ops@example.test"])
    sender = FakeSender()
    healthy = {
        "+61400000101": _number(),
        "+61400000102": {"inbound_agents": [{"agent_id": "agent_test_b", "agent_version": 1}]},
    }
    # agent_test_b has no published versions in the fake: drift on the second number.
    assert await voice_config.tick(monitor, ops_engine, FakeApi(healthy), sender) == "failing"
    assert monitor.report()["numbers"] == {
        "+61400000101": "ok",
        "+61400000102": "unpublished_version",
    }
    [(_to, _subject, body)] = sender.sent
    assert "+61400000102: unpublished_version" in body and "+61400000101" not in body
    await voice_config.tick(monitor, ops_engine, FakeApi(healthy), sender)
    assert len(sender.sent) == 1  # not re-sent every run


async def test_health_endpoint() -> None:
    settings = OpsWorkerSettings(
        environment=Environment.TEST, scheduler_enabled=False, retell_api_key="k"
    )
    app = build_app(settings, engine=None, retell=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://o") as client:
        assert (await client.get("/health/voice-config")).status_code == 503  # never checked
        app.state.voice_config.watch.record({"status": "ok", "numbers": {}, "checked": 0})
        assert (await client.get("/health/voice-config")).status_code == 200
    unconfigured = build_app(
        OpsWorkerSettings(environment=Environment.TEST, scheduler_enabled=False),
        engine=None,
        retell=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unconfigured), base_url="http://o"
    ) as client:
        assert (await client.get("/health/voice-config")).json() == {"status": "unconfigured"}


async def test_retell_client_reads_numbers_and_exact_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.raw_path.decode()))
        if "get-phone-number" in request.url.path:
            if request.url.raw_path.endswith(b"%2B61400000999"):
                return httpx.Response(404, json={})
            return httpx.Response(200, json=_number())
        asked = int(request.url.params["version"])
        # version 9 simulates the API ignoring the parameter and returning the latest draft
        got = 10 if asked == 9 else asked
        return httpx.Response(200, json={"version": got, "is_published": True, "webhook_url": HOOK})

    real = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    client = RetellClient("k")
    assert await client.get_phone_number("+61400000999") is None
    number = await client.get_phone_number("+61400000101")
    assert number is not None and number["inbound_agents"][0]["agent_version"] == 3
    assert (await client.get_agent_version("agent_test_a", 3)) == {
        "version": 3,
        "is_published": True,
        "webhook_url": HOOK,
    }
    assert await client.get_agent_version("agent_test_a", 9) is None  # never trust another version
    assert seen[0] == "/get-phone-number/%2B61400000999"  # '+' is encoded, not a space
