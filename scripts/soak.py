"""Staging soak: steady synthetic traffic through the phone path, plus health checks.

Every ``--every`` seconds for ``--hours``, against the seeded synthetic clinic
(``db/seed_synthetic.py``: agent ``agent_staging_synthetic``, line ``+61400000999``):

- a signed ``call_analyzed`` webhook for a new synthetic call, then the same webhook again
  (exactly-once: both must be accepted, one call stored);
- a signed ``capture_message`` tool call (never urgent: no alert email) and a
  ``lookup_patient`` for a name nobody has (callers rotate over 50 synthetic numbers, so the
  per-caller limit is exercised but never exhausted);
- every ``/health`` URL given in ``SOAK_HEALTH_URLS``.

Each cycle appends one JSON line (statuses and latencies only) to ``--log``; the run ends with a
summary and exits non-zero if anything failed. It signs with the voice-gateway's key, which
``railway run`` injects without anyone seeing it:

    railway run -s voice-gateway -e staging -- uv run python scripts/soak.py --hours 24

It refuses to run against production.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
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
DEFAULT_HEALTH = ",".join(
    [
        "https://voice-gateway-staging-be0b.up.railway.app/health",
        "https://core-api-staging-202d.up.railway.app/health",
        "https://ops-worker-staging.up.railway.app/health",
        "https://ops-worker-staging.up.railway.app/health/replay",
        "https://ops-worker-staging.up.railway.app/health/backup",
    ]
)


def _signed(client: httpx.Client, url: str, payload: dict[str, Any], key: str) -> httpx.Response:
    body = json.dumps(payload).encode()
    header = sign(body, key, int(time.time() * 1000))
    return client.post(
        url,
        content=body,
        headers={"content-type": "application/json", "x-retell-signature": header},
    )


def cycle(
    client: httpx.Client, gateway: str, key: str, n: int, health: list[str]
) -> dict[str, Any]:
    call_id = f"call_soak_{uuid.uuid4().hex}"
    caller = f"+614000009{n % 50:02d}"
    now_ms = int(time.time() * 1000)
    call = {
        "call_id": call_id,
        "agent_id": AGENT_ID,
        "direction": "inbound",
        "from_number": caller,
        "to_number": CLINIC_LINE,
        "start_timestamp": now_ms - 60_000,
        "end_timestamp": now_ms,
        "duration_ms": 60_000,
        "call_cost": {"combined_cost": 12.5},
        "disconnection_reason": "user_hangup",
        "transcript": "Agent: Synthetic soak call.\nUser: Synthetic soak caller.",
        "call_analysis": {
            "call_summary": "Synthetic soak-test call (staging).",
            "user_sentiment": "Neutral",
            "call_successful": True,
            "custom_analysis_data": {"intent": "soak_test"},
        },
    }
    results: dict[str, Any] = {}

    def timed(name: str, expect: set[int], fn: Any) -> httpx.Response | None:
        started = time.perf_counter()
        try:
            response = fn()
        except httpx.HTTPError as exc:
            results[name] = {"ok": False, "error": type(exc).__name__}
            return None
        ms = round((time.perf_counter() - started) * 1000)
        results[name] = {
            "ok": response.status_code in expect,
            "status": response.status_code,
            "ms": ms,
        }
        return response

    webhook = f"{gateway}/v1/retell/webhook"
    envelope = {"event": "call_analyzed", "call": call}
    timed("webhook", {204}, lambda: _signed(client, webhook, envelope, key))
    timed("webhook_retry", {204}, lambda: _signed(client, webhook, envelope, key))
    tools = f"{gateway}/v1/retell/tools/{CLINIC_SLUG}"
    message = timed(
        "capture_message",
        {200},
        lambda: _signed(
            client,
            f"{tools}/capture_message",
            {
                "name": "capture_message",
                "call": call,
                "args": {"category": "general", "detail": "Synthetic soak message."},
            },
            key,
        ),
    )
    if message is not None and message.status_code == 200 and message.json().get("ok") is not True:
        results["capture_message"]["ok"] = False  # the fallback answered: the write failed
    lookup = timed(
        "lookup_patient",
        {200},
        lambda: _signed(
            client,
            f"{tools}/lookup_patient",
            {
                "name": "lookup_patient",
                "call": call,
                "args": {
                    "first_name": "Nobody",
                    "last_name": "Soaktest",
                    "date_of_birth": "1901-01-01",
                },
            },
            key,
        ),
    )
    if (
        lookup is not None
        and lookup.status_code == 200
        and lookup.json().get("matched") is not False
    ):
        results["lookup_patient"]["ok"] = False
    for url in health:
        timed(f"health {url.split('//', 1)[1]}", {200}, lambda url=url: client.get(url))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--every", type=float, default=600.0, help="seconds between cycles")
    parser.add_argument("--log", default="soak.jsonl")
    args = parser.parse_args()
    if os.environ.get("WASSUP_ENVIRONMENT", "") == "production":
        print("refused: the soak is for staging only", file=sys.stderr)
        return 2
    key = os.environ.get("WASSUP_RETELL_API_KEY", "")
    if not key:
        print("WASSUP_RETELL_API_KEY is required (use railway run)", file=sys.stderr)
        return 2
    gateway = os.environ.get("SOAK_VOICE_GATEWAY", DEFAULT_GATEWAY).rstrip("/")
    health = [u for u in os.environ.get("SOAK_HEALTH_URLS", DEFAULT_HEALTH).split(",") if u]
    deadline = time.time() + args.hours * 3600
    failures: dict[str, int] = defaultdict(int)
    latencies: dict[str, list[int]] = defaultdict(list)
    cycles = 0
    with httpx.Client(timeout=15.0) as client, Path(args.log).open("a") as log:
        while True:
            results = cycle(client, gateway, key, cycles, health)
            cycles += 1
            for name, result in results.items():
                if not result["ok"]:
                    failures[name] += 1
                if "ms" in result:
                    latencies[name].append(result["ms"])
            bad = [name for name, result in results.items() if not result["ok"]]
            log.write(
                json.dumps({"at": int(time.time()), "cycle": cycles, "results": results}) + "\n"
            )
            log.flush()
            print(f"cycle {cycles}: {'ok' if not bad else 'FAILED ' + ', '.join(bad)}", flush=True)
            if time.time() + args.every > deadline:
                break
            time.sleep(args.every)
    print(f"\nsoak finished: {cycles} cycles, {sum(failures.values())} failures")
    for name in sorted(latencies):
        values = sorted(latencies[name])
        p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
        print(
            f"  {name}: failures={failures.get(name, 0)} "
            f"median_ms={statistics.median(values):.0f} p95_ms={p95}"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
