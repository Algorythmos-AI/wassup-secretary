"""core-api: staff and dashboard API (/v1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.app import create_app
from wassup_core.db import make_engine

from core_api.analytics import router as analytics_router
from core_api.auth import TokenVerifier, build_verifier
from core_api.routes import router
from core_api.settings import CoreApiSettings


def build_app(
    settings: CoreApiSettings | None = None,
    engine: AsyncEngine | None = None,
    verifier: TokenVerifier | None = None,
) -> FastAPI:
    settings = settings or CoreApiSettings()
    app = create_app(settings, [router, analytics_router])
    app.state.engine = engine
    if verifier is None and (settings.auth_mode == "test" or settings.firebase_project_id):
        verifier = build_verifier(settings)  # raises if test auth is used outside local/test
    app.state.verifier = verifier  # None → every protected route answers 503, never "open"
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match"],
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        owned = None
        if app.state.engine is None and settings.database_url is not None:
            owned = make_engine(
                settings.database_url.get_secret_value(),
                pool_size=settings.db_pool_size,
                statement_timeout_ms=settings.db_statement_timeout_ms,
            )
            app.state.engine = owned
        try:
            yield
        finally:
            if owned is not None:
                await owned.dispose()

    app.router.lifespan_context = lifespan
    return app


app = build_app()
