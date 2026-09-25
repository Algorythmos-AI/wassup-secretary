"""Structured JSON logging with an allowlist redactor.

Health data flows through every service, so logging is deny-by-default: only keys in
``ALLOWED_KEYS`` are emitted as-is. Any other key keeps its name but its value is replaced
with ``"[redacted]"``. Adding a key to the allowlist is a reviewed code change, which is the
point: a developer can't leak a transcript, name, date of birth or phone number into logs by
passing it as a keyword argument.

Exceptions are logged as their type and code location only, never their message: a database
error's message carries its DETAIL line ("Key (phone)=(+614…) already exists"), and validation
errors echo the input. That applies to library loggers too (uvicorn prints a traceback for every
unhandled error), so the standard-library root logger goes through the same pipeline.
"""

from __future__ import annotations

import logging
import sys
import traceback
from collections.abc import MutableMapping
from pathlib import Path
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
        "event_id",
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
        "exception",
    }
)

# Library loggers that log request URLs or payloads at INFO; only their warnings are kept.
_QUIET_LOGGERS = ("httpx", "httpcore", "sqlalchemy", "asyncio")
_MAX_FRAMES = 8


def redact_disallowed(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: replace the value of every non-allowlisted key."""
    for key in list(event_dict.keys()):
        if key not in ALLOWED_KEYS:
            event_dict[key] = REDACTED
    return event_dict


def describe_exception(exc: BaseException) -> str:
    """Type and code location of an exception and its causes, without any message text."""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        frames = traceback.extract_tb(current.__traceback__)[-_MAX_FRAMES:]
        where = " < ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in reversed(frames))
        name = f"{type(current).__module__}.{type(current).__qualname__}"
        parts.append(f"{name} at {where}" if where else name)
        current = current.__cause__ or current.__context__
    return " <- caused by ".join(parts)


def safe_exception(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: replace ``exc_info`` with :func:`describe_exception`."""
    exc_info = event_dict.pop("exc_info", None)
    exc: BaseException | None = None
    if isinstance(exc_info, BaseException):
        exc = exc_info
    elif isinstance(exc_info, tuple) and len(exc_info) == 3:
        exc = exc_info[1]
    elif exc_info:
        exc = sys.exc_info()[1]
    if exc is not None:
        event_dict["exception"] = describe_exception(exc)
    return event_dict


def configure_logging(service: str, level: str = "INFO") -> None:
    """Configure JSON logs on stdout for one service. Safe to call more than once."""
    level_name = level.upper()
    tail: list[Any] = [redact_disallowed, safe_exception, structlog.processors.JSONRenderer()]

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.add_logger_name,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
            ],
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *tail],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level_name)
    # uvicorn installs its own handlers (plain-text tracebacks); route it through ours instead.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # uvicorn's access log is never wanted: it prints full URLs (query strings included) and
    # client addresses, and our own request log records what we need. uvicorn decides whether
    # to write it by asking whether this logger has handlers, so it must have none and must not
    # propagate to ours, whatever --no-access-log says.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            *tail,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level_name)),
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
