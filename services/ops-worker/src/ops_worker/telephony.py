"""Telephony account monitor: the check that would have caught the September 2026 outage.

Every clinic call reaches the voice agent through one telephony account. In September 2026 that
account was suspended for an unpaid balance and every line went silent for 13 days, with nothing
alerting anyone. This job checks, every few minutes:

- the account's status (``active`` / ``suspended`` / ``closed``);
- its balance against a floor (auto-recharge can fail: an expired card, a declined payment);
- that each AI line number is still owned by the account (a released number that the clinic's PBX
  still diverts to is someone else's phone line).

A change to ``failing`` emails ops immediately (and again every ``REALERT_EVERY`` while it stays
failing); ``/health/telephony`` serves the latest result for an external monitor, and is itself
``failing`` if no check has succeeded recently (a dead job must not look healthy).

The account API returns the account's auth token among other fields: only ``status`` is read and
nothing from the response is ever logged or stored.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx
from wassup_core.logging import get_logger

from ops_worker.notifier import EmailSender

log = get_logger(__name__)

API = "https://api.twilio.com/2010-04-01"
REALERT_EVERY_S = 6 * 3600
# The endpoint reports 'failing' when the last successful check is older than this.
STALE_AFTER_S = 3600


class TelephonyUnavailable(Exception):
    """The provider's API could not be reached or answered unexpectedly (not proof of a problem)."""


class TelephonyRejected(Exception):
    """The provider refused our credentials: revoked key, or the account itself is unusable."""


class TelephonyApi(Protocol):
    async def account_status(self) -> str: ...
    async def balance(self) -> tuple[Decimal, str]: ...
    async def owns_number(self, e164: str) -> bool: ...


class TwilioClient:
    """Read-only calls with an API key (never the account's master auth token)."""

    def __init__(
        self, account_sid: str, key_sid: str, key_secret: str, client: httpx.AsyncClient
    ) -> None:
        self._account = account_sid
        self._auth = (key_sid, key_secret)
        self._client = client

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{API}/Accounts/{self._account}{path}", params=params, auth=self._auth
            )
        except httpx.HTTPError as exc:
            raise TelephonyUnavailable(type(exc).__name__) from exc
        if response.status_code in (401, 403):
            raise TelephonyRejected(f"http_{response.status_code}")
        if response.status_code != 200:
            raise TelephonyUnavailable(f"http_{response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise TelephonyUnavailable("bad_json") from exc
        if not isinstance(body, dict):
            raise TelephonyUnavailable("bad_json")
        return body

    async def account_status(self) -> str:
        return str((await self._get(".json")).get("status") or "unknown")

    async def balance(self) -> tuple[Decimal, str]:
        body = await self._get("/Balance.json")
        try:
            return Decimal(str(body.get("balance"))), str(body.get("currency") or "")
        except InvalidOperation as exc:
            raise TelephonyUnavailable("bad_balance") from exc

    async def owns_number(self, e164: str) -> bool:
        body = await self._get("/IncomingPhoneNumbers.json", {"PhoneNumber": e164})
        numbers = body.get("incoming_phone_numbers")
        return isinstance(numbers, list) and any(
            isinstance(n, dict) and n.get("phone_number") == e164 for n in numbers
        )


@dataclass(frozen=True)
class TelephonyConfig:
    lines: list[str]
    min_balance: Decimal
    ops_emails: list[str]


@dataclass
class TelephonyMonitor:
    """Holds the latest result between the scheduled job and the health endpoint."""

    config: TelephonyConfig
    latest: dict[str, Any] | None = None
    last_success: float | None = None
    _alerted_status: str | None = field(default=None, repr=False)
    _alerted_at: float = field(default=0.0, repr=False)

    def report(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        if self.latest is None or self.last_success is None:
            return {"status": "failing", "reason": "never_checked"}
        if now - self.last_success > STALE_AFTER_S:
            return {**self.latest, "status": "failing", "reason": "stale"}
        return self.latest


async def check(api: TelephonyApi, config: TelephonyConfig) -> dict[str, Any]:
    """One check. Returns the report; raises TelephonyUnavailable if nothing could be learnt."""
    problems: list[str] = []
    try:
        status = await api.account_status()
    except TelephonyRejected as exc:
        return {"status": "failing", "reason": f"credentials_rejected:{exc}", "problems": []}
    if status != "active":
        problems.append(f"account_{status}")
    report: dict[str, Any] = {"account": status}
    try:
        amount, currency = await api.balance()
        report.update(balance=str(amount), currency=currency)
        if amount < config.min_balance:
            problems.append("balance_low")
    except TelephonyRejected as exc:
        problems.append(f"balance_rejected:{exc}")
    missing = []
    try:
        for line in config.lines:
            if not await api.owns_number(line):
                missing.append(line)
    except TelephonyRejected as exc:
        problems.append(f"numbers_rejected:{exc}")
    if missing:
        problems.append("numbers_missing")
        report["missing_numbers"] = missing  # our own public line numbers, not personal data
    report["problems"] = problems
    report["status"] = "failing" if problems else "ok"
    return report


async def tick(monitor: TelephonyMonitor, api: TelephonyApi, email: EmailSender) -> str:
    """Scheduled job: check, remember, alert on a change to failing (and periodically after)."""
    try:
        report = await check(api, monitor.config)
    except TelephonyUnavailable as exc:
        log.warning("telephony_check_unavailable", reason=str(exc))
        return "unknown"
    now = time.time()
    monitor.latest, monitor.last_success = report, now
    status = report["status"]
    if status == "failing":
        due = monitor._alerted_status != "failing" or now - monitor._alerted_at > REALERT_EVERY_S
        if due:
            await _alert(email, monitor.config.ops_emails, report)
            monitor._alerted_at = now
        log.error(
            "telephony_failing",
            reason=",".join(report.get("problems") or [report.get("reason", "")]),
        )
    elif monitor._alerted_status == "failing":
        log.info("telephony_recovered")
    monitor._alerted_status = status
    return str(status)


async def _alert(email: EmailSender, ops_emails: list[str], report: dict[str, Any]) -> None:
    problems = report.get("problems") or [report.get("reason", "unknown")]
    body = "\n".join(
        [
            "The telephony account behind every clinic's AI line needs attention NOW.",
            "If the account is suspended or out of credit, calls to the AI lines fail silently.",
            "",
            f"Problems: {', '.join(problems)}",
            f"Account status: {report.get('account', 'unknown')}",
            f"Balance: {report.get('balance', '?')} {report.get('currency', '')}",
            "Numbers not found on the account: "
            + (", ".join(report.get("missing_numbers", [])) or "none"),
            "",
            "Runbook: docs/runbooks/phone-line-down.md",
        ]
    )
    if not ops_emails:
        log.error("telephony_failing_nobody_alerted")
        return
    try:
        await email.send(ops_emails, "WASSUP telephony account needs attention", body)
    except Exception as exc:
        log.error("telephony_alert_failed", code=type(exc).__name__)
