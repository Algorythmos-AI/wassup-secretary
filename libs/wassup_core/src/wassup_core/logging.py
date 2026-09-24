"""Structured JSON logging with an allowlist redactor.

Health data flows through every service, so logging is deny-by-default: only keys in
``ALLOWED_KEYS`` are emitted as-is. Any other key keeps its name but its value is replaced
with ``"[redacted]"``. Adding a key to the allowlist is a reviewed code change, which is the
point: a developer can't leak a transcript, name, date of birth or phone number into logs by
passing it as a keyword argument.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

REDACTED = "[redacted]"

# Keys whose values are safe to log verbatim: identifiers, codes, counts and timings.
ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        # structlog / logging internals
        "event",
        "level",
        "timestamp",
        "logger",
        "exc_info",
        "stack_info",
        # service identity
        "service",
        "environment",
        "version",
        "git_tree",
        # request correlation
        "request_id",
        "method",
        "path",
        "route",
        "status",
        "duration_ms",
        # domain identifiers (opaque, non-personal)
        "clinic_id",
        "call_id",
        "event_type",
        "tool",
        "dedupe_key",
        "agent_id",
        "job",
        # outcomes and counts
        "outcome",
        "reason",
        "code",
        "count",
        "attempt",
        "limit_bytes",
    }
)


def redact_disallowed(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: replace the value of every non-allowlisted key."""
    for key in list(event_dict.keys()):
        if key not in ALLOWED_KEYS:
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(service: str, level: str = "INFO") -> None:
    """Configure JSON logs on stdout for one service. Safe to call more than once."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper(), force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_disallowed,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
