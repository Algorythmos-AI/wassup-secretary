import json

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
