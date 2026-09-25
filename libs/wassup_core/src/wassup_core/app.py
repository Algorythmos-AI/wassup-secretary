"""FastAPI application factory shared by every service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from wassup_core.db import unscoped
from wassup_core.http import BodyLimitMiddleware, install_problem_handlers, install_request_logging
from wassup_core.logging import configure_logging, get_logger
from wassup_core.settings import BaseServiceSettings

DEFAULT_BODY_LIMIT = 256 * 1024

# Returns None when the service can serve traffic, or a short reason code when it can't yet.
Readiness = Callable[[FastAPI], Awaitable[str | None]]
log = get_logger(__name__)


def schema_readiness(probe_sql: str) -> Readiness:
    """Ready once ``probe_sql`` (a fixed privilege/existence check on the newest schema object
    the service relies on) returns true on ``app.state.engine``. No engine configured counts as
    ready: the routes themselves then answer 503."""
    probe = text(probe_sql)

    async def check(app: FastAPI) -> str | None:
        engine: AsyncEngine | None = app.state.engine
        if engine is None:
            return None
        try:
            async with unscoped(engine) as conn:
                allowed = (await conn.execute(probe)).scalar()
        except Exception as exc:  # e.g. the column or table doesn't exist yet
            log.warning("schema_not_ready", code=type(exc).__name__)
            return "schema_behind"
        return None if allowed else "schema_behind"

    return check


def create_app(
    settings: BaseServiceSettings,
    routers: Sequence[APIRouter] = (),
    body_limits: Mapping[str, int] | None = None,
    default_body_limit: int = DEFAULT_BODY_LIMIT,
    readiness: Readiness | None = None,
) -> FastAPI:
    """``readiness`` gates ``/health``: the deploy platform only routes traffic to (and only
    finishes deploying) a replica whose health check passes. Services use it to refuse traffic
    until the database schema they need is in place: migrations run in a different service's
    pre-deploy step, so a deploy order is never guaranteed. Once ready, it is not re-checked."""
    configure_logging(settings.service_name, settings.log_level)
    docs = settings.expose_api_docs
    app = FastAPI(
        title=settings.service_name,
        version=settings.version,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    install_problem_handlers(app)
    install_request_logging(app)
    app.add_middleware(
        BodyLimitMiddleware, limits=dict(body_limits or {}), default_limit=default_body_limit
    )

    health = APIRouter()

    ready = readiness is None

    @health.get("/health", include_in_schema=False, response_model=None)
    async def _health() -> dict[str, str] | JSONResponse:
        nonlocal ready
        body = {
            "status": "ok",
            "service": settings.service_name,
            "environment": settings.environment.value,
            "version": settings.version,
            "tree": settings.git_tree,
        }
        if not ready and readiness is not None:
            reason = await readiness(app)
            if reason is not None:
                return JSONResponse({**body, "status": "not_ready", "reason": reason}, 503)
            ready = True
        return body

    app.include_router(health)
    for router in routers:
        app.include_router(router)
    return app
