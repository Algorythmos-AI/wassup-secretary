"""State shared by the external-configuration monitors (telephony account, voice bindings).

A monitor's job runs on a schedule and records each successful check here; its /health endpoint
serves the latest result from memory (so the unauthenticated endpoint never calls a provider).
The result is 'failing' when the last check failed, when no check has succeeded for
``stale_after_s`` (a dead job must not look healthy), or when no check has run yet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Watch:
    stale_after_s: float
    realert_every_s: float
    latest: dict[str, Any] | None = None
    last_success: float | None = None
    _alerted_status: str | None = field(default=None, repr=False)
    _alerted_at: float = field(default=0.0, repr=False)

    def report(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        if self.latest is None or self.last_success is None:
            return {"status": "failing", "reason": "never_checked"}
        if now - self.last_success > self.stale_after_s:
            return {**self.latest, "status": "failing", "reason": "stale"}
        return self.latest

    def record(self, report: dict[str, Any]) -> tuple[bool, bool]:
        """Store a completed check. Returns (alert_due, recovered): alert on a change to
        failing and again every ``realert_every_s`` while it stays failing."""
        now = time.time()
        self.latest, self.last_success = report, now
        status = report["status"]
        due = status == "failing" and (
            self._alerted_status != "failing" or now - self._alerted_at > self.realert_every_s
        )
        if due:
            self._alerted_at = now
        recovered = status != "failing" and self._alerted_status == "failing"
        self._alerted_status = status
        return due, recovered
