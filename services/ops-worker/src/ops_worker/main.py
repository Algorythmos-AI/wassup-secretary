"""ops-worker: scheduled and background jobs. Exposes /health endpoints for monitors."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.app import create_app
from wassup_core.backups import BackupError, Store, key_from_hex, store_from_env
from wassup_core.db import make_engine

from ops_worker import backup, canary, quarantine, replay, retention, telephony, usage, voice_config
from ops_worker.health_routes import install_probes
from ops_worker.health_routes import router as health_router
from ops_worker.notifier import EmailSender, NotConfiguredSender, ResendEmailSender
from ops_worker.outbox import process_batch
from ops_worker.retell_api import RetellApi, RetellClient
from ops_worker.scheduler import run_every
from ops_worker.settings import OpsWorkerSettings


def email_sender(settings: OpsWorkerSettings) -> EmailSender:
    if settings.resend_api_key and settings.alert_email_from:
        return ResendEmailSender(
            settings.resend_api_key.get_secret_value(), settings.alert_email_from
        )
    return NotConfiguredSender()


def canary_config(settings: OpsWorkerSettings) -> canary.CanaryConfig:
    return canary.CanaryConfig(
        enabled=settings.canary_enabled,
        agent_id=settings.canary_agent_id,
        agent_version=settings.canary_agent_version,
        local_time=settings.canary_local_time,
        timezone=settings.canary_timezone,
        lines=settings.ai_lines,
        ops_emails=settings.ops_emails,
    )


Job = tuple[str, float, Callable[[], Awaitable[object]], str]


def backup_setup(
    settings: OpsWorkerSettings,
) -> tuple[backup.BackupMonitor | None, Store | None, bytes | None]:
    """(monitor, store, key). No monitor when backups aren't configured at all. A configuration
    that asks for backups but can't work gives a monitor that reports ``failing`` with the reason,
    and never stops the rest of the worker."""
    error: str | None = None
    try:
        store = store_from_env(dict(os.environ))
    except BackupError:
        store, error = None, "store_misconfigured"
    wants_run = settings.backup_database_url is not None or settings.backup_key_hex is not None
    if store is None and not wants_run and error is None:
        return None, None, None
    key: bytes | None = None
    mode = "run" if wants_run else "watch"
    if wants_run:
        if settings.backup_database_url is None or settings.backup_key_hex is None:
            error = error or "needs_both_database_url_and_key"
        else:
            try:
                key = key_from_hex(settings.backup_key_hex.get_secret_value())
            except BackupError:
                error = error or "key_invalid"
        if store is None:
            error = error or "no_store"
    monitor = backup.BackupMonitor(
        backup.BackupConfig(
            environment=settings.environment.value,
            local_time=settings.backup_local_time,
            timezone=settings.backup_timezone,
            keep=settings.backup_keep,
            ops_emails=settings.ops_emails,
            heartbeat_url=settings.backup_heartbeat_url,
            on_start=settings.backup_on_start,
            mode=mode,
        ),
        config_error=error,
    )
    return monitor, store, key


def _backup_job(app: FastAPI, settings: OpsWorkerSettings, sender: EmailSender) -> Job | None:
    """The backup job: backs up in run mode, re-reads the store in watch mode."""
    backups: backup.BackupMonitor | None = app.state.backup
    backup_store: Store | None = app.state.backup_store
    if backups is not None and backup_store is not None and backups.config_error is None:
        live_backup, live_store = backups, backup_store
        run_backup: Callable[[], Awaitable[dict[str, Any]]] | None = None
        interval = backup.WATCH_INTERVAL_S
        if backups.config.mode == "run":
            backup_url = settings.backup_database_url.get_secret_value()  # type: ignore[union-attr]
            backup_key: bytes = app.state.backup_key

            async def run_backup_now() -> dict[str, Any]:
                return await asyncio.to_thread(
                    backup.backup_once,
                    backup_url,
                    backup_key,
                    live_store,
                    live_backup.config.environment,
                    keep=live_backup.config.keep,
                )

            run_backup, interval = run_backup_now, 60.0

        async def backup_job() -> str:
            return await backup.tick(live_backup, live_store, run_backup, sender)

        return ("backup", interval, backup_job, "")  # the heartbeat is pinged by the job
    return None


def _scheduled_jobs(
    app: FastAPI, settings: OpsWorkerSettings, clients: list[httpx.AsyncClient]
) -> list[Job]:
    """Every background job, with its interval and external heartbeat URL. Jobs whose settings
    are missing are simply not scheduled (their /health endpoint reports that)."""
    eng: AsyncEngine = app.state.engine
    sender = email_sender(settings)
    ops = settings.ops_emails

    async def outbox_job() -> int:
        return await process_batch(
            eng,
            sender,
            batch_size=settings.outbox_batch_size,
            dashboard_url=settings.dashboard_url,
            ops_emails=ops,
        )

    async def retention_job() -> dict[str, int]:
        return await retention.run(eng, timedelta(days=settings.raw_retention_days))

    live_quarantine: quarantine.QuarantineMonitor = app.state.quarantine

    async def quarantine_job() -> str:
        return await quarantine.tick(live_quarantine, eng, sender)

    async def usage_job() -> int:
        return await usage.run(eng)

    jobs: list[Job] = [
        ("outbox", settings.outbox_interval_s, outbox_job, settings.outbox_heartbeat_url),
        ("usage", settings.usage_interval_s, usage_job, settings.usage_heartbeat_url),
        ("quarantine", 300.0, quarantine_job, settings.quarantine_heartbeat_url),
        (
            "retention",
            settings.retention_interval_s,
            retention_job,
            settings.retention_heartbeat_url,
        ),
    ]

    if settings.voice_gateway_url and settings.retell_api_key is not None:
        gateway = replay.Gateway(
            base_url=settings.voice_gateway_url,
            sign=replay.retell_signer(settings.retell_api_key.get_secret_value()),
            client=httpx.AsyncClient(timeout=10.0),
        )
        clients.append(gateway.client)

        async def replay_job() -> dict[str, int]:
            return await replay.run(eng, gateway, sender, ops)

        jobs.append(
            ("replay", settings.replay_interval_s, replay_job, settings.replay_heartbeat_url)
        )

    monitor: telephony.TelephonyMonitor | None = app.state.telephony
    if monitor is not None and settings.twilio_api_key_secret is not None:
        twilio_http = httpx.AsyncClient(timeout=15.0)
        clients.append(twilio_http)
        twilio = telephony.TwilioClient(
            settings.twilio_account_sid,
            settings.twilio_api_key_sid,
            settings.twilio_api_key_secret.get_secret_value(),
            twilio_http,
        )
        live_monitor = monitor

        async def telephony_job() -> str:
            return await telephony.tick(live_monitor, twilio, sender)

        jobs.append(
            (
                "telephony",
                settings.telephony_interval_s,
                telephony_job,
                settings.telephony_heartbeat_url,
            )
        )

    drift: voice_config.VoiceConfigMonitor | None = app.state.voice_config
    if drift is not None and settings.retell_api_key is not None:
        config_api = RetellClient(settings.retell_api_key.get_secret_value())
        live_drift = drift

        async def voice_config_job() -> str:
            return await voice_config.tick(live_drift, eng, config_api, sender)

        jobs.append(
            (
                "voice_config",
                settings.voice_config_interval_s,
                voice_config_job,
                settings.voice_config_heartbeat_url,
            )
        )

    backup_job = _backup_job(app, settings, sender)
    if backup_job is not None:
        jobs.append(backup_job)

    cfg: canary.CanaryConfig = app.state.canary_config
    live_retell: RetellApi | None = app.state.retell
    if cfg.enabled and live_retell is not None:
        retell_api: RetellApi = live_retell

        async def canary_job() -> dict[str, int]:
            return await canary.tick(eng, retell_api, sender, cfg, datetime.now(UTC))

        jobs.append(("canary", 60.0, canary_job, settings.canary_heartbeat_url))
    return jobs


def build_app(
    settings: OpsWorkerSettings | None = None,
    engine: AsyncEngine | None = None,
    retell: RetellApi | None = None,
) -> FastAPI:
    settings = settings or OpsWorkerSettings()
    app = create_app(settings, [health_router])
    app.state.engine = engine
    app.state.canary_config = canary_config(settings)
    if retell is None and settings.retell_api_key is not None:
        retell = RetellClient(settings.retell_api_key.get_secret_value())
    app.state.retell = retell
    install_probes(app.state)
    app.state.quarantine = quarantine.QuarantineMonitor(settings.ops_emails)
    app.state.backup, app.state.backup_store, app.state.backup_key = backup_setup(settings)
    app.state.voice_config = (
        voice_config.VoiceConfigMonitor(
            environment="production" if settings.is_production else "staging",
            webhook_url=settings.voice_webhook_url,
            ops_emails=settings.ops_emails,
        )
        if settings.retell_api_key is not None
        else None
    )
    app.state.telephony = (
        telephony.TelephonyMonitor(
            telephony.TelephonyConfig(
                lines=settings.ai_lines,
                min_balance=settings.telephony_min_balance,
                ops_emails=settings.ops_emails,
            )
        )
        if settings.telephony_configured
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        owned = None
        if app.state.engine is None and settings.database_url is not None:
            owned = make_engine(
                settings.database_url.get_secret_value(), pool_size=settings.db_pool_size
            )
            app.state.engine = owned
        tasks: list[asyncio.Task[None]] = []
        clients: list[httpx.AsyncClient] = []
        if settings.scheduler_enabled and app.state.engine is not None:
            for name, interval_s, job, heartbeat in _scheduled_jobs(app, settings, clients):
                tasks.append(asyncio.create_task(run_every(name, interval_s, job, heartbeat)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            for client in clients:
                await client.aclose()
            if owned is not None:
                await owned.dispose()

    app.router.lifespan_context = lifespan
    return app


app = build_app()
