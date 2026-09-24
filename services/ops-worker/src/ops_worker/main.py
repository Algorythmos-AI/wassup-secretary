"""ops-worker: scheduled and background jobs. Exposes /health endpoints for monitors."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.app import create_app
from wassup_core.db import make_engine

from ops_worker import canary, replay
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
            sender = email_sender(settings)
            eng: AsyncEngine = app.state.engine

            async def outbox_job() -> int:
                return await process_batch(
                    eng,
                    sender,
                    batch_size=settings.outbox_batch_size,
                    dashboard_url=settings.dashboard_url,
                    ops_emails=settings.ops_emails,
                )

            tasks.append(
                asyncio.create_task(
                    run_every(
                        "outbox",
                        settings.outbox_interval_s,
                        outbox_job,
                        settings.outbox_heartbeat_url,
                    )
                )
            )
            if settings.voice_gateway_url and settings.retell_api_key is not None:
                gateway = replay.Gateway(
                    base_url=settings.voice_gateway_url,
                    sign=replay.retell_signer(settings.retell_api_key.get_secret_value()),
                    client=httpx.AsyncClient(timeout=10.0),
                )
                clients.append(gateway.client)

                async def replay_job() -> dict[str, int]:
                    return await replay.run(eng, gateway, sender, settings.ops_emails)

                tasks.append(
                    asyncio.create_task(
                        run_every(
                            "replay",
                            settings.replay_interval_s,
                            replay_job,
                            settings.replay_heartbeat_url,
                        )
                    )
                )
            cfg: canary.CanaryConfig = app.state.canary_config
            live_retell: RetellApi | None = app.state.retell
            if cfg.enabled and live_retell is not None:
                retell_api: RetellApi = live_retell

                async def canary_job() -> dict[str, int]:
                    return await canary.tick(eng, retell_api, sender, cfg, datetime.now(UTC))

                tasks.append(
                    asyncio.create_task(
                        run_every("canary", 60.0, canary_job, settings.canary_heartbeat_url)
                    )
                )
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
