"""Telephony account monitor: the check that would have caught the September 2026 suspension."""

from __future__ import annotations

import base64
from decimal import Decimal
from typing import Any

import httpx
import pytest
from ops_worker import telephony, watch
from ops_worker.main import build_app
from ops_worker.settings import OpsWorkerSettings
from ops_worker.telephony import TelephonyConfig, TelephonyMonitor, TwilioClient
from wassup_core.settings import Environment

LINES = ["+61200000001", "+61200000002"]
ACCOUNT = "AC" + "0" * 32


class FakeApi:
    def __init__(
        self, status: str = "active", balance: str = "50.00", owned: list[str] | None = None
    ) -> None:
        self.status, self._balance = status, Decimal(balance)
        self.owned = LINES if owned is None else owned
        self.unavailable = False

    async def account_status(self) -> str:
        if self.unavailable:
            raise telephony.TelephonyUnavailable("ConnectError")
        return self.status

    async def balance(self) -> tuple[Decimal, str]:
        return self._balance, "USD"

    async def owns_number(self, e164: str) -> bool:
        return e164 in self.owned


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str, str]] = []

    async def send(self, to: list[str], subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


def _monitor() -> TelephonyMonitor:
    return TelephonyMonitor(TelephonyConfig(LINES, Decimal(20), ["ops@example.test"]))


@pytest.mark.parametrize(
    ("api", "problems"),
    [
        (FakeApi(), []),
        (FakeApi(status="suspended"), ["account_suspended"]),  # September 2026
        (FakeApi(status="closed"), ["account_closed"]),
        (FakeApi(balance="9.14"), ["balance_low"]),
        (FakeApi(owned=LINES[:1]), ["numbers_missing"]),
    ],
)
async def test_check_verdicts(api: FakeApi, problems: list[str]) -> None:
    report = await telephony.check(api, _monitor().config)
    assert report["problems"] == problems
    assert report["status"] == ("failing" if problems else "ok")


async def test_alerts_once_on_failure_then_again_only_after_the_realert_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor, sender, api = _monitor(), FakeSender(), FakeApi(status="suspended")
    clock = [1_000_000.0]
    monkeypatch.setattr(watch.time, "time", lambda: clock[0])
    assert await telephony.tick(monitor, api, sender) == "failing"
    clock[0] += 300
    await telephony.tick(monitor, api, sender)
    assert len(sender.sent) == 1
    assert "account_suspended" in sender.sent[0][2]
    clock[0] += telephony.REALERT_EVERY_S + 1
    await telephony.tick(monitor, api, sender)
    assert len(sender.sent) == 2
    api.status = "active"
    assert await telephony.tick(monitor, api, sender) == "ok"
    api.status = "suspended"
    await telephony.tick(monitor, api, sender)
    assert len(sender.sent) == 3  # a new failure after recovery alerts straight away


async def test_unreachable_provider_is_unknown_then_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor, api = _monitor(), FakeApi()
    clock = [1_000_000.0]
    monkeypatch.setattr(watch.time, "time", lambda: clock[0])
    assert monitor.report()["reason"] == "never_checked"
    await telephony.tick(monitor, api, FakeSender())
    assert monitor.report()["status"] == "ok"
    api.unavailable = True
    clock[0] += telephony.STALE_AFTER_S + 1
    with pytest.raises(telephony.TelephonyUnavailable):  # the job fails: no heartbeat ping
        await telephony.tick(monitor, api, FakeSender())
    assert monitor.report() | {} == {**monitor.report(), "status": "failing", "reason": "stale"}


def _twilio(handler: Any) -> TwilioClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TwilioClient(ACCOUNT, "SK_test", "secret_test", client)


async def test_twilio_client_reads_only_what_it_needs() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith(f"/Accounts/{ACCOUNT}.json"):
            return httpx.Response(
                200, json={"status": "suspended", "auth_token": "MASTER-TOKEN-MUST-NOT-LEAK"}
            )
        if request.url.path.endswith("/Balance.json"):
            return httpx.Response(200, json={"balance": "9.14", "currency": "USD"})
        number = request.url.params["PhoneNumber"]
        owned = [{"phone_number": number}] if number == LINES[0] else []
        return httpx.Response(200, json={"incoming_phone_numbers": owned})

    report = await telephony.check(_twilio(handler), _monitor().config)
    assert report == {
        "account": "suspended",
        "balance": "9.14",
        "currency": "USD",
        "missing_numbers": [LINES[1]],
        "problems": ["account_suspended", "balance_low", "numbers_missing"],
        "status": "failing",
    }
    assert "MASTER-TOKEN" not in str(report)
    expected = "Basic " + base64.b64encode(b"SK_test:secret_test").decode()
    assert {r.headers["authorization"] for r in seen} == {expected}  # API key, not the auth token


async def test_rejected_credentials_are_a_failure() -> None:
    report = await telephony.check(
        _twilio(lambda _r: httpx.Response(401, json={})), _monitor().config
    )
    assert (report["status"], report["reason"]) == ("failing", "credentials_rejected:http_401")


async def test_health_endpoint_states() -> None:
    base = {"environment": Environment.TEST, "scheduler_enabled": False}
    unconfigured = build_app(OpsWorkerSettings(**base), engine=None, retell=None)
    configured = build_app(
        OpsWorkerSettings(
            **base,
            twilio_account_sid=ACCOUNT,
            twilio_api_key_sid="SK_test",
            twilio_api_key_secret="secret",
            ai_line_numbers=",".join(LINES),
        ),
        engine=None,
        retell=None,
    )
    for app, code in ((unconfigured, 503), (configured, 503)):  # never checked is not healthy
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://o"
        ) as client:
            assert (await client.get("/health/telephony")).status_code == code
    await telephony.tick(configured.state.telephony, FakeApi(), FakeSender())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=configured), base_url="http://o"
    ) as client:
        response = await client.get("/health/telephony")
    assert response.status_code == 200 and response.json()["status"] == "ok"


async def test_health_report_hides_the_balance() -> None:
    monitor = _monitor()
    await telephony.tick(monitor, FakeApi(balance="9.14"), FakeSender())
    report = monitor.report()
    assert report["problems"] == ["balance_low"]
    assert "balance" not in report and "currency" not in report
