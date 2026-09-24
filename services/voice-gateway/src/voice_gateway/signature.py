"""Verification of Retell request signatures.

Header ``x-retell-signature: v=<unix-ms>,d=<hex>`` where ``d`` is HMAC-SHA256, keyed with the
Retell API key, over the raw request body followed by the timestamp. Verification:
- runs on the exact raw bytes (never on re-serialised JSON),
- accepts the current key or, during a rotation, the previous key,
- rejects timestamps outside ``tolerance_s`` (replay protection),
- compares in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Sequence

DEFAULT_TOLERANCE_S = 300


def _parse(header: str) -> tuple[int, str] | None:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, digest = parts.get("v"), parts.get("d")
    if not ts or not digest or not ts.isdigit():
        return None
    return int(ts), digest.lower()


def sign(body: bytes, key: str, timestamp_ms: int) -> str:
    """Produce a header value (used by tests and by the staging replay tool)."""
    mac = hmac.new(key.encode(), body + str(timestamp_ms).encode(), hashlib.sha256).hexdigest()
    return f"v={timestamp_ms},d={mac}"


def verify(
    body: bytes,
    header: str | None,
    keys: Sequence[str],
    *,
    now_ms: int | None = None,
    tolerance_s: int = DEFAULT_TOLERANCE_S,
) -> bool:
    if not header or not keys:
        return False
    parsed = _parse(header)
    if parsed is None:
        return False
    timestamp_ms, digest = parsed
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(now_ms - timestamp_ms) > tolerance_s * 1000:
        return False
    message = body + str(timestamp_ms).encode()
    ok = False
    for key in keys:
        expected = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
        # Evaluate every key so timing doesn't reveal which one matched.
        ok = hmac.compare_digest(expected, digest) or ok
    return ok
