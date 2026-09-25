"""voice-gateway: the only service the voice provider calls (signed webhooks and tool calls)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.app import create_app, schema_readiness
from wassup_core.db import make_engine

from voice_gateway.rules import RulesCache
from voice_gateway.settings import VoiceGatewaySettings
from voice_gateway.tools import router as tools_router
from voice_gateway.webhook import router as webhook_router

# Sized from production traffic: a full call_analyzed payload was p50 17 KB, p99 87 KB,
# max 154 KB (Sept 2026). Limits leave >30x headroom so a long call is never dropped.
WEBHOOK_BODY_LIMIT = 5 * 1024 * 1024
TOOL_BODY_LIMIT = 2 * 1024 * 1024
# The newest schema object this service's code relies on (migration 0012). Bump it together with
# the migration that adds something voice-gateway needs: /health stays 503 until it exists.
SCHEMA_PROBE = "SELECT has_column_privilege('tool_invocations', 'caller_number', 'INSERT')"


def build_app(
    settings: VoiceGatewaySettings | None = None, engine: AsyncEngine | None = None
) -> FastAPI:
    settings = settings or VoiceGatewaySettings()
    app = create_app(
        settings,
        [webhook_router, tools_router],
        body_limits={"/v1/retell/webhook": WEBHOOK_BODY_LIMIT, "/v1/retell/tools": TOOL_BODY_LIMIT},
        readiness=schema_readiness(SCHEMA_PROBE),
    )
    app.state.engine = engine
    app.state.rules = RulesCache()  # each clinic's active classification rules, briefly cached

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        owned = None
        if app.state.engine is None and settings.database_url is not None:
            owned = make_engine(
                settings.database_url.get_secret_value(),
                pool_size=settings.db_pool_size,
                pool_timeout_s=settings.db_pool_timeout_s,
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
