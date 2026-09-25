from fastapi.testclient import TestClient
from ops_worker.main import build_app


def test_health() -> None:
    body = TestClient(build_app()).get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "ops-worker"
