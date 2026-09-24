"""FastAPI application factory shared by every service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fastapi import APIRouter, FastAPI

from wassup_core.http import BodyLimitMiddleware, install_problem_handlers, install_request_logging
from wassup_core.logging import configure_logging
from wassup_core.settings import BaseServiceSettings

DEFAULT_BODY_LIMIT = 256 * 1024


def create_app(
    settings: BaseServiceSettings,
    routers: Sequence[APIRouter] = (),
    body_limits: Mapping[str, int] | None = None,
    default_body_limit: int = DEFAULT_BODY_LIMIT,
) -> FastAPI:
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

    @health.get("/health", include_in_schema=False)
    async def _health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": settings.service_name,
            "environment": settings.environment.value,
            "version": settings.version,
            "tree": settings.git_tree,
        }

    app.include_router(health)
    for router in routers:
        app.include_router(router)
    return app
