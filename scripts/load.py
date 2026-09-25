"""Load test for the phone path: many simultaneous synthetic calls against staging voice-gateway.

``--callers`` virtual calls run at once (default 60 = 3 times the planned peak of 20 concurrent
calls). Each repeatedly does what a real call does, back to back with ``--think`` seconds
between steps: a ``capture_message`` tool call, a ``lookup_patient`` for a name nobody has,
then its signed ``call_analyzed`` webhook (sent twice, as a retry would). It runs for
``--minutes`` and reports per-step counts, errors, and p50/p95/p99 latency measured from here
(so network time to the platform is included). Exit is non-zero when any request failed or the
tool p95 is over ``--p95-ms`` (default 800, the readiness gate).

    railway run -s voice-gateway -e staging -- uv run python scripts/load.py --minutes 5

It refuses production and uses the seeded synthetic clinic only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/voice-gateway/src"))
from voice_gateway.signature import sign

AGENT_ID = "agent_staging_synthetic"
CLINIC_LINE = "+61400000999"
CLINIC_SLUG = "staging-clinic"
DEFAULT_GATEWAY = "https://voice-gateway-staging-be0b.up.railway.app"


class Stats:
    def __init__(self) -> None:
        self.ms: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def add(self, step: str, ms: float, error: str | None) -> None:
        self.ms[step].append(ms)
        if error:
            self.errors[step][error] += 1

    def pct(self, step: str, q: float) -> float:
        values = sorted(self.ms[step])
        return values[min(len(values) - 1, int(len(values) * q))] if values else 0.0


async def _post(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any], key: str
) -> httpx.Response:
    body = json.dumps(payload).encode()
    header = sign(body, key, int(time.time() * 1000))
    return await client.post(
        url,
        content=body,
        headers={"content-type": "application/json", "x-retell-signature": header},
    )


async def caller(
    n: int,
    client: httpx.AsyncClient,
    *,
    gateway: str,
    key: str,
    stats: Stats,
    deadline: float,
    think: float,
) -> None:
    tools = f"{gateway}/v1/retell/tools/{CLINIC_SLUG}"
    while time.time() < deadline:
        now_ms = int(time.time() * 1000)
        call = {
            "call_id": f"call_load_{uuid.uuid4().hex}",
            "agent_id": AGENT_ID,
            "direction": "inbound",
            "from_number": f"+61400001{n % 1000:03d}",
            "to_number": CLINIC_LINE,
            "start_timestamp": now_ms,
        }
        steps: list[tuple[str, str, dict[str, Any], Any]] = [
            (
                "capture_message",
                f"{tools}/capture_message",
                {
                    "name": "capture_message",
                    "call": call,
                    "args": {"category": "general", "detail": "Synthetic load-test message."},
                },
                lambda r: r.status_code == 200 and r.json().get("ok") is True,
            ),
            (
                "lookup_patient",
                f"{tools}/lookup_patient",
                {
                    "name": "lookup_patient",
                    "call": call,
                    "args": {
                        "first_name": "Nobody",
                        "last_name": "Loadtest",
                        "date_of_birth": "1901-01-01",
                    },
                },
                lambda r: r.status_code == 200 and "degraded" not in r.json(),
            ),
        ]
        analyzed = {
            **call,
            "end_timestamp": now_ms + 60_000,
            "duration_ms": 60_000,
            "call_analysis": {
                "call_summary": "Synthetic load-test call.",
                "custom_analysis_data": {"intent": "load_test"},
            },
        }
        envelope = {"event": "call_analyzed", "call": analyzed}
        for name in ("webhook", "webhook_retry"):
            steps.append(
                (name, f"{gateway}/v1/retell/webhook", envelope, lambda r: r.status_code == 204)
            )
        for step, url, payload, good in steps:
            started = time.perf_counter()
            error: str | None = None
            try:
                response = await _post(client, url, payload, key)
                if not good(response):
                    error = f"status_{response.status_code}"
            except httpx.HTTPError as exc:
                error = type(exc).__name__
            stats.add(step, (time.perf_counter() - started) * 1000, error)
            await asyncio.sleep(think)


async def run(args: argparse.Namespace, key: str) -> int:
    gateway = os.environ.get("SOAK_VOICE_GATEWAY", DEFAULT_GATEWAY).rstrip("/")
    stats = Stats()
    deadline = time.time() + args.minutes * 60
    limits = httpx.Limits(max_connections=args.callers, max_keepalive_connections=args.callers)
    async with httpx.AsyncClient(timeout=15.0, limits=limits) as client:
        await asyncio.gather(
            *[
                caller(
                    n,
                    client,
                    gateway=gateway,
                    key=key,
                    stats=stats,
                    deadline=deadline,
                    think=args.think,
                )
                for n in range(args.callers)
            ]
        )
    total = sum(len(v) for v in stats.ms.values())
    failed = sum(sum(e.values()) for e in stats.errors.values())
    print(f"load: {args.callers} callers for {args.minutes} min: {total} requests, {failed} failed")
    for step in sorted(stats.ms):
        errors = dict(stats.errors.get(step, {}))
        print(
            f"  {step}: n={len(stats.ms[step])} p50={stats.pct(step, 0.5):.0f}ms "
            f"p95={stats.pct(step, 0.95):.0f}ms p99={stats.pct(step, 0.99):.0f}ms errors={errors}"
        )
    tool_p95 = max(stats.pct(s, 0.95) for s in ("capture_message", "lookup_patient"))
    if failed or tool_p95 > args.p95_ms:
        print(f"FAIL (tool p95 {tool_p95:.0f}ms, limit {args.p95_ms}ms; failures {failed})")
        return 1
    print(f"PASS (tool p95 {tool_p95:.0f}ms <= {args.p95_ms}ms, no failures)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--callers", type=int, default=60)
    parser.add_argument("--minutes", type=float, default=5.0)
    parser.add_argument("--think", type=float, default=2.0, help="seconds between a call's steps")
    parser.add_argument("--p95-ms", type=float, default=800.0)
    args = parser.parse_args()
    if os.environ.get("WASSUP_ENVIRONMENT", "") == "production":
        print("refused: load tests are for staging only", file=sys.stderr)
        return 2
    key = os.environ.get("WASSUP_RETELL_API_KEY", "")
    if not key:
        print("WASSUP_RETELL_API_KEY is required (use railway run)", file=sys.stderr)
        return 2
    return asyncio.run(run(args, key))


if __name__ == "__main__":
    raise SystemExit(main())
