"""ops-worker settings (environment only)."""

from __future__ import annotations

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
