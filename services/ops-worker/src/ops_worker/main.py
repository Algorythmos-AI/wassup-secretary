"""ops-worker: scheduled and background jobs. Exposes /health endpoints for monitors."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.app import create_app
from wassup_core.db import make_engine

from ops_worker import canary, replay, retention, telephony
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

    jobs: list[Job] = [
        ("outbox", settings.outbox_interval_s, outbox_job, settings.outbox_heartbeat_url),
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
