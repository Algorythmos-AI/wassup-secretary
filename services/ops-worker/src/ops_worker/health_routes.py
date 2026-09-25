"""Monitor endpoints. Counts, states and public line numbers only — never personal data.

They are unauthenticated (monitors must reach them), so each is a cached, single-flight probe:
however many requests arrive, the database and the voice provider see at most one query per
probe per cache period.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import State

from ops_worker import canary, outbox, reconcile, replay

router = APIRouter()

Compute = Callable[[State], Awaitable[dict[str, Any]]]


class CachedProbe:
    """Serve a recent result; recompute at most once per ``ttl_s``, one caller at a time. An
    exception is not cached, so a probe that fails (e.g. database down) is retried next time."""

    def __init__(self, ttl_s: float, compute: Compute) -> None:
        self.ttl_s = ttl_s
        self.compute = compute
        self._lock = asyncio.Lock()
        self._value: dict[str, Any] | None = None
        self._at = 0.0

    async def get(self, state: State) -> dict[str, Any]:
        async with self._lock:
            if self._value is None or time.monotonic() - self._at > self.ttl_s:
                self._value = await self.compute(state)
                self._at = time.monotonic()
            return self._value


async def _canary(state: State) -> dict[str, Any]:
    return await canary.status(state.engine, state.canary_config, datetime.now(UTC))


async def _freshness(state: State) -> dict[str, Any]:
    return await reconcile.ingestion_gap(
        state.engine, state.retell, set(state.canary_config.lines), datetime.now(UTC)
    )


async def _outbox(state: State) -> dict[str, Any]:
    return await outbox.health(state.engine, timedelta(minutes=10))


async def _replay(state: State) -> dict[str, Any]:
    return await replay.health(state.engine)


def install_probes(state: State) -> None:
    state.probes = {
        "canary": CachedProbe(30.0, _canary),
        "freshness": CachedProbe(60.0, _freshness),  # calls the voice provider's API
        "outbox": CachedProbe(15.0, _outbox),
        "replay": CachedProbe(15.0, _replay),
    }


def _respond(report: dict[str, Any], failing: bool) -> JSONResponse:
    return JSONResponse(report, status_code=503 if failing else 200)


@router.get("/health/canary", include_in_schema=False)
async def canary_health(request: Request) -> JSONResponse:
    state = request.app.state
    if state.engine is None:
        return JSONResponse({"status": "unconfigured"}, status_code=503)
    report = await state.probes["canary"].get(state)
    return _respond(report, report["status"] == "failing")


@router.get("/health/freshness", include_in_schema=False)
async def freshness_health(request: Request) -> JSONResponse:
    """503 only on a proven gap; 'unknown' (provider unreachable) is reported, not paged."""
    state = request.app.state
    if state.engine is None or state.retell is None:
        return JSONResponse({"ingestion": "unconfigured"}, status_code=503)
    report = await state.probes["freshness"].get(state)
    return _respond(report, report.get("ingestion") == "gap")


@router.get("/health/outbox", include_in_schema=False)
async def outbox_health(request: Request) -> JSONResponse:
    """503 while anything is dead-lettered, overdue or failing repeatedly (alerts not going out)."""
    state = request.app.state
    if state.engine is None:
        return JSONResponse({"status": "unconfigured"}, status_code=503)
    report = await state.probes["outbox"].get(state)
    return _respond(report, report["status"] == "failing")


@router.get("/health/replay", include_in_schema=False)
async def replay_health(request: Request) -> JSONResponse:
    """503 while a stored webhook event or tool request could not be processed."""
    state = request.app.state
    if state.engine is None:
        return JSONResponse({"status": "unconfigured"}, status_code=503)
    report = await state.probes["replay"].get(state)
    return _respond(report, report["status"] == "failing")


@router.get("/health/telephony", include_in_schema=False)
async def telephony_health(request: Request) -> JSONResponse:
    """The latest telephony account check (served from memory: this endpoint never calls the
    provider). 503 when failing, stale, or not configured — an unwatched account is a risk."""
    monitor = request.app.state.telephony
    if monitor is None:
        return JSONResponse({"status": "unconfigured"}, status_code=503)
    report = monitor.report()
    return _respond(report, report["status"] != "ok")


@router.get("/health/voice-config", include_in_schema=False)
async def voice_config_health(request: Request) -> JSONResponse:
    """Latest voice-binding check, from memory. 503 when a number drifted, the check is stale or
    never ran, or the voice provider isn't configured."""
    monitor = request.app.state.voice_config
    if monitor is None:
        return JSONResponse({"status": "unconfigured"}, status_code=503)
    report = monitor.report()
    return _respond(report, report["status"] != "ok")


@router.get("/health/quarantine", include_in_schema=False)
async def quarantine_health(request: Request) -> JSONResponse:
    """Latest quarantine check, from memory: 503 while any quarantined item is unresolved (a
    quarantined call is on no dashboard), or when the check is stale or never ran."""
    report = request.app.state.quarantine.report()
    return _respond(report, report["status"] != "ok")


@router.get("/health/backup", include_in_schema=False)
async def backup_health(request: Request) -> JSONResponse:
    """The latest backup, from memory (counts and names only). 503 when the last one failed,
    none has been made for 36 hours, or backups aren't configured."""
    monitor = request.app.state.backup
    if monitor is None:
        return JSONResponse({"status": "unconfigured"}, status_code=503)
    report = monitor.report()
    return _respond(report, report["status"] == "failing")
