"""Tiny in-process scheduler: each job runs on a fixed interval, never overlapping itself.

After each successful run a job may ping an external heartbeat URL (e.g. Better Stack), so a
dead or stuck worker raises an alert on its own — the watcher is itself watched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
from wassup_core.logging import get_logger

log = get_logger(__name__)


async def ping(url: str) -> None:
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.get(url)
    except httpx.HTTPError:
        log.warning("heartbeat_failed", job="heartbeat")


async def run_every(
    name: str, interval_s: float, job: Callable[[], Awaitable[object]], heartbeat_url: str = ""
) -> None:
    while True:
        try:
            await job()
            await ping(heartbeat_url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failing job must not kill the scheduler
            log.error("job_failed", job=name, code=type(exc).__name__)
        await asyncio.sleep(interval_s)
