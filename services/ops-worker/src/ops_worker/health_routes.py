"""Monitor endpoints. Counts, states and public line numbers only — never personal data."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ops_worker import canary, reconcile

router = APIRouter()
_FRESHNESS_CACHE_S = 60.0


@router.get("/health/canary", include_in_schema=False)
async def canary_health(request: Request) -> JSONResponse:
    state = request.app.state
    if state.engine is None:
        return JSONResponse({"status": "unconfigured"}, status_code=503)
    report = await canary.status(state.engine, state.canary_config, datetime.now(UTC))
    return JSONResponse(report, status_code=503 if report["status"] == "failing" else 200)


@router.get("/health/freshness", include_in_schema=False)
async def freshness_health(request: Request) -> JSONResponse:
    """503 only on a proven gap; 'unknown' (provider unreachable) is reported, not paged."""
    state = request.app.state
    if state.engine is None or state.retell is None:
        return JSONResponse({"ingestion": "unconfigured"}, status_code=503)
    cached: tuple[float, dict[str, Any]] | None = getattr(state, "freshness_cache", None)
    if cached is None or time.monotonic() - cached[0] > _FRESHNESS_CACHE_S:
        report = await reconcile.ingestion_gap(
            state.engine, state.retell, set(state.canary_config.lines), datetime.now(UTC)
        )
        cached = (time.monotonic(), report)
        state.freshness_cache = cached
    report = cached[1]
    return JSONResponse(report, status_code=503 if report.get("ingestion") == "gap" else 200)
