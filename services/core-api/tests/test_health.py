from core_api.main import build_app
from fastapi.testclient import TestClient


def test_health() -> None:
    body = TestClient(build_app()).get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "core-api"
