from fastapi.testclient import TestClient
from voice_gateway.main import TOOL_BODY_LIMIT, WEBHOOK_BODY_LIMIT, build_app


def test_health() -> None:
    body = TestClient(build_app()).get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "voice-gateway"


def test_webhook_route_allows_large_call_payloads_but_default_does_not() -> None:
    client = TestClient(build_app(), raise_server_exceptions=False)
    big = b"x" * (300 * 1024)  # larger than a default route allows, far below the webhook limit
    # Unknown route under the webhook prefix → 404 (not 413): the body limit let it through.
    assert client.post("/v1/retell/webhook-probe", content=big).status_code == 404
    assert client.post("/v1/other", content=big).status_code == 413
    assert WEBHOOK_BODY_LIMIT > TOOL_BODY_LIMIT > 256 * 1024
