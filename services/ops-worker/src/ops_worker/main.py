"""ops-worker: scheduled and background jobs. Exposes /health for the platform and monitors."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.app import create_app
from wassup_core.db import make_engine

from ops_worker.notifier import EmailSender, NotConfiguredSender, ResendEmailSender
from ops_worker.outbox import process_batch
from ops_worker.scheduler import run_every
from ops_worker.settings import OpsWorkerSettings


def email_sender(settings: OpsWorkerSettings) -> EmailSender:
    if settings.resend_api_key and settings.alert_email_from:
        return ResendEmailSender(
            settings.resend_api_key.get_secret_value(), settings.alert_email_from
        )
    return NotConfiguredSender()


def build_app(
    settings: OpsWorkerSettings | None = None, engine: AsyncEngine | None = None
) -> FastAPI:
    settings = settings or OpsWorkerSettings()
    app = create_app(settings)
    app.state.engine = engine

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        owned = None
        if app.state.engine is None and settings.database_url is not None:
            owned = make_engine(
                settings.database_url.get_secret_value(), pool_size=settings.db_pool_size
            )
            app.state.engine = owned
        tasks: list[asyncio.Task[None]] = []
        if settings.scheduler_enabled and app.state.engine is not None:
            sender = email_sender(settings)
            eng = app.state.engine

            async def outbox_job() -> int:
                return await process_batch(
                    eng,
                    sender,
                    batch_size=settings.outbox_batch_size,
                    dashboard_url=settings.dashboard_url,
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
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if owned is not None:
                await owned.dispose()

    app.router.lifespan_context = lifespan
    return app


app = build_app()
