import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel
from wassup_core.app import create_app
from wassup_core.settings import BaseServiceSettings, Environment


class Echo(BaseModel):
    value: str


def _router() -> APIRouter:
    router = APIRouter()

    @router.post("/small")
    async def small(body: Echo) -> dict[str, int]:
        return {"length": len(body.value)}

    @router.post("/big/upload")
    async def big(body: Echo) -> dict[str, int]:
        return {"length": len(body.value)}

    @router.get("/boom")
    async def boom() -> None:
        raise RuntimeError("database password=hunter2 exploded")

    return router


def _client(env: Environment = Environment.TEST) -> TestClient:
    settings = BaseServiceSettings(
        service_name="svc", environment=env, version="1.2.3", git_tree="abc"
    )
    app = create_app(settings, [_router()], body_limits={"/big": 10_000}, default_body_limit=1_000)
    return TestClient(app, raise_server_exceptions=False)


def test_health_reports_build_identity() -> None:
    body = _client().get("/health").json()
    assert body == {
        "status": "ok",
        "service": "svc",
        "environment": "test",
        "version": "1.2.3",
        "tree": "abc",
    }


def test_docs_hidden_in_production() -> None:
    client = _client(Environment.PRODUCTION)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_docs_available_in_test() -> None:
    assert _client().get("/openapi.json").status_code == 200


def test_500_is_generic_problem_without_internal_text() -> None:
    response = _client().get("/boom")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "hunter2" not in response.text
    assert response.json()["code"] == "internal_error"


def test_validation_error_does_not_echo_values() -> None:
    response = _client().post("/small", json={"value": 12345678901234567890, "extra": "secret-ish"})
    assert response.status_code == 422
    assert "12345678901234567890" not in response.text


def test_body_over_default_limit_is_413() -> None:
    response = _client().post("/small", json={"value": "x" * 2_000})
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_per_prefix_limit_allows_larger_body() -> None:
    response = _client().post("/big/upload", json={"value": "x" * 5_000})
    assert response.status_code == 200
    assert response.json() == {"length": 5_000}


def test_streamed_body_without_content_length_is_still_limited() -> None:
    def chunks():  # type: ignore[no-untyped-def]
        yield b'{"value": "'
        yield b"x" * 5_000
        yield b'"}'

    response = _client().post(
        "/small", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413


def test_environment_defaults_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that forgets WASSUP_ENVIRONMENT must fail closed."""
    monkeypatch.delenv("WASSUP_ENVIRONMENT", raising=False)
    settings = BaseServiceSettings()
    assert settings.environment is Environment.PRODUCTION
    assert not settings.expose_api_docs


def test_health_waits_for_readiness_then_stays_ready() -> None:
    """A service whose schema isn't in place yet must fail its health check, so the platform
    doesn't route traffic to it; once ready, it isn't re-checked on every probe."""
    answers = ["schema_behind", None]
    calls: list[int] = []

    async def readiness(_app: object) -> str | None:
        calls.append(1)
        return answers.pop(0) if answers else "should_not_be_asked_again"

    settings = BaseServiceSettings(service_name="svc", environment=Environment.TEST)
    client = TestClient(create_app(settings, readiness=readiness))  # type: ignore[arg-type]
    first = client.get("/health")
    assert first.status_code == 503
    assert (first.json()["status"], first.json()["reason"]) == ("not_ready", "schema_behind")
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200
    assert len(calls) == 2
