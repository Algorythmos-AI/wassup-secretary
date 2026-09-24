"""ops-worker settings (environment only)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import SecretStr
from wassup_core.settings import BaseServiceSettings


class OpsWorkerSettings(BaseServiceSettings):
    service_name: str = "ops-worker"
    scheduler_enabled: bool = True
    outbox_interval_s: float = 5.0
    outbox_batch_size: int = 20
    # Email for alerts goes through Resend, deliberately not the telephony provider (the
    # September 2026 outage was the telephony account itself).
    resend_api_key: SecretStr | None = None
    alert_email_from: str = ""
    dashboard_url: str = ""
    # Optional external heartbeat (e.g. Better Stack) pinged after each successful outbox cycle.
    outbox_heartbeat_url: str = ""
    db_pool_size: int = 3

    # Voice provider (read calls; place line-check calls).
    retell_api_key: SecretStr | None = None
    # Our AI line numbers, comma-separated E.164. Each line is rung daily from another one.
    ai_line_numbers: str = ""
    ops_alert_emails: str = ""
    # Daily line check ("canary"): off until the telephony provider allows the test calls.
    canary_enabled: bool = False
    canary_agent_id: str = ""
    canary_agent_version: int | None = None
    canary_local_time: str = "07:30"
    canary_timezone: str = "Australia/Sydney"
    canary_heartbeat_url: str = ""
    reconcile_interval_s: float = 600.0
    # Replay of stored-but-unprocessed webhook events and tool requests: voice-gateway's base URL
    # on the private network (e.g. http://voice-gateway.railway.internal:8080). Unset = off (the
    # /health/replay monitor still reports what is waiting).
    voice_gateway_url: str = ""
    replay_interval_s: float = 30.0
    replay_heartbeat_url: str = ""
    # Telephony account monitor (the September 2026 outage was this account being suspended).
    # Use a read-only API key, never the account's master auth token.
    twilio_account_sid: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: SecretStr | None = None
    # Alert below this balance (in the account's currency) even if auto-recharge is on.
    telephony_min_balance: Decimal = Decimal(20)
    telephony_interval_s: float = 300.0
    telephony_heartbeat_url: str = ""
    # Voice configuration drift: the webhook URL every published agent version must post to
    # (e.g. https://<voice-gateway>/v1/retell/webhook). Empty = the webhook is not compared.
    voice_webhook_url: str = ""
    voice_config_interval_s: float = 900.0
    voice_config_heartbeat_url: str = ""
    quarantine_heartbeat_url: str = ""
    # Raw provider payloads (verbatim caller content) are deleted this long after they are finished.
    raw_retention_days: int = 90
    retention_interval_s: float = 6 * 3600
    retention_heartbeat_url: str = ""

    @property
    def telephony_configured(self) -> bool:
        return bool(
            self.twilio_account_sid and self.twilio_api_key_sid and self.twilio_api_key_secret
        )

    @property
    def ai_lines(self) -> list[str]:
        return [n.strip() for n in self.ai_line_numbers.split(",") if n.strip()]

    @property
    def ops_emails(self) -> list[str]:
        return [e.strip() for e in self.ops_alert_emails.split(",") if e.strip()]
