import json
import logging

import pytest
import structlog
from wassup_core.logging import REDACTED, configure_logging, get_logger, redact_disallowed


def test_non_allowlisted_values_are_redacted() -> None:
    event = {
        "event": "call_stored",
        "call_id": "c1",
        "caller_name": "Test Patient",
        "dob": "1990-01-01",
    }
    out = redact_disallowed(None, "info", event)
    assert out["call_id"] == "c1"
    assert out["caller_name"] == REDACTED
    assert out["dob"] == REDACTED


def test_rendered_log_never_contains_personal_values(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("test-service")
    get_logger().info("lookup", call_id="c2", phone="+61400000999", transcript="I feel unwell")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["call_id"] == "c2"
    assert record["service"] == "test-service"
    assert "+61400000999" not in line
    assert "I feel unwell" not in line
    structlog.reset_defaults()


def _raise_with_personal_data() -> None:
    try:
        raise ValueError("Key (phone)=(+61400000999) already exists")
    except ValueError as inner:
        raise RuntimeError("lookup failed for Test Patient") from inner


def test_exceptions_are_logged_without_their_message(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("test-service")
    try:
        _raise_with_personal_data()
    except RuntimeError:
        get_logger().exception("failed", call_id="c3")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert "+61400000999" not in line and "Test Patient" not in line
    assert record["exception"].startswith("builtins.RuntimeError at test_logging.py:")
    assert "caused by builtins.ValueError" in record["exception"]
    structlog.reset_defaults()


def test_library_tracebacks_are_redacted_too(capsys: pytest.CaptureFixture[str]) -> None:
    """uvicorn logs every unhandled error with a full traceback through the stdlib logger."""
    configure_logging("test-service")
    try:
        _raise_with_personal_data()
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    logging.getLogger("httpx").info("HTTP Request: GET https://api.example.test/?phone=+614000")
    out = capsys.readouterr().out
    assert "+61400000999" not in out and "Test Patient" not in out and "+614000" not in out
    record = json.loads(out.strip().splitlines()[-1])
    assert record["logger"] == "uvicorn.error"
    assert record["event"] == "Exception in ASGI application"
    assert "builtins.RuntimeError" in record["exception"]
    structlog.reset_defaults()


def test_uvicorn_access_log_stays_off_even_after_configuring() -> None:
    """uvicorn writes access lines (full URL, query string, client address) whenever its access
    logger has handlers; configuring our logging must never give it any."""
    import logging as std_logging  # noqa: PLC0415

    from wassup_core.logging import configure_logging  # noqa: PLC0415

    configure_logging("test-service")
    access = std_logging.getLogger("uvicorn.access")
    assert not access.hasHandlers() or access.disabled
    assert access.propagate is False
    assert std_logging.getLogger("uvicorn.error").propagate is True  # startup/errors still flow
