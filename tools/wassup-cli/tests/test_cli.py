"""The operator CLI against a fake voice provider (an in-memory httpx transport)."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from wassup_cli.main import CliError, main, run
from wassup_cli.retell import Retell

NUMBER = "+61238211140"


class FakeProvider:
    """Numbers and agent versions; records every write."""

    def __init__(self) -> None:
        self.numbers: dict[str, dict[str, Any]] = {
            NUMBER: {"phone_number": NUMBER, "inbound_agents": [_route("agent_a", 1)]},
            "+61255011140": {
                "phone_number": "+61255011140",
                "inbound_agents": [{"agent_id": "agent_b", "agent_version": None, "weight": 1}],
            },
        }
        self.versions = {
            ("agent_a", 1): {"is_published": True, "response_engine": _engine("llm_a", 1)},
            ("agent_a", 2): {"is_published": True, "response_engine": _engine("llm_a", 2)},
            ("agent_a", 3): {"is_published": False},
        }
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.ignore_writes = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/list-phone-numbers":
            return httpx.Response(200, json=list(self.numbers.values()))
        if path.startswith("/get-phone-number/"):
            number = self.numbers.get(path.rsplit("/", 1)[1])
            return httpx.Response(200 if number else 404, json=number or {})
        if path.startswith("/get-agent/"):
            key = (path.rsplit("/", 1)[1], int(request.url.params["version"]))
            if key not in self.versions:
                return httpx.Response(404, json={})
            return httpx.Response(
                200, json={"agent_id": key[0], "version": key[1], **self.versions[key]}
            )
        if path.startswith("/get-retell-llm/"):
            return httpx.Response(200, json={"general_prompt": "SECRET PROMPT"})
        if path.startswith("/update-phone-number/") and request.method == "PATCH":
            number = path.rsplit("/", 1)[1]
            body = json.loads(request.content)
            self.writes.append((number, body))
            if not self.ignore_writes:
                self.numbers[number]["inbound_agents"] = body["inbound_agents"]
            return httpx.Response(200, json=self.numbers[number])
        return httpx.Response(500, json={})


def _route(agent: str, version: int | None) -> dict[str, Any]:
    return {"agent_id": agent, "agent_version": version, "weight": 1}


def _engine(llm: str, version: int) -> dict[str, Any]:
    return {"type": "retell-llm", "llm_id": llm, "version": version}


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


def _api(provider: FakeProvider) -> Retell:
    client = httpx.Client(
        base_url="https://api.example", transport=httpx.MockTransport(provider.handle)
    )
    return Retell("test-key", client)


def _run(provider: FakeProvider, *argv: str) -> str:
    out = io.StringIO()
    run(list(argv), _api(provider), out)
    return out.getvalue()


def test_bindings_flag_floating_and_unpublished(provider: FakeProvider) -> None:
    provider.numbers[NUMBER]["inbound_agents"] = [_route("agent_a", 3)]
    text = _run(provider, "voice", "bindings")
    assert f"{NUMBER}: agent_a v3  !! agent_a v3 is NOT published" in text
    assert "+61255011140: agent_b vLATEST  !! agent_b is on LATEST (floating)" in text


def test_export_writes_private_files(provider: FakeProvider, tmp_path: Path) -> None:
    out_dir = tmp_path / "export"
    _run(provider, "voice", "export", "--out", str(out_dir))
    names = sorted(p.name for p in out_dir.iterdir())
    assert names == ["agent_a.v1.agent.json", "agent_a.v1.llm.json", "phone-numbers.json"]
    assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in out_dir.iterdir())


def test_rebind_is_a_dry_run_by_default(provider: FakeProvider, tmp_path: Path) -> None:
    text = _run(
        provider,
        "voice",
        "rebind",
        NUMBER,
        "--agent",
        "agent_a",
        "--version",
        "2",
        "--state-dir",
        str(tmp_path),
    )
    assert "dry run" in text and provider.writes == []


def test_rebind_refuses_drafts_and_unknown_versions(provider: FakeProvider, tmp_path: Path) -> None:
    for version, message in (("3", "is a draft"), ("9", "does not exist")):
        with pytest.raises(CliError, match=message):
            _run(
                provider,
                "voice",
                "rebind",
                NUMBER,
                "--agent",
                "agent_a",
                "--version",
                version,
                "--apply",
                "--state-dir",
                str(tmp_path),
            )
    assert provider.writes == []


def test_rebind_then_rollback(provider: FakeProvider, tmp_path: Path) -> None:
    state = ["--state-dir", str(tmp_path)]
    text = _run(
        provider,
        "voice",
        "rebind",
        NUMBER,
        "--agent",
        "agent_a",
        "--version",
        "2",
        "--apply",
        *state,
    )
    assert "applied and verified" in text
    assert provider.writes == [(NUMBER, {"inbound_agents": [_route("agent_a", 2)]})]
    assert provider.numbers[NUMBER]["inbound_agents"] == [_route("agent_a", 2)]

    _run(provider, "voice", "rollback", NUMBER, "--apply", *state)
    assert provider.numbers[NUMBER]["inbound_agents"] == [_route("agent_a", 1)]
    records = [json.loads(x) for x in (tmp_path / "61238211140.jsonl").read_text().splitlines()]
    assert [r["reason"] for r in records] == ["manual", "rollback"]


def test_a_write_that_did_not_stick_is_reported(provider: FakeProvider, tmp_path: Path) -> None:
    provider.ignore_writes = True
    with pytest.raises(CliError, match="verification FAILED"):
        _run(
            provider,
            "voice",
            "rebind",
            NUMBER,
            "--agent",
            "agent_a",
            "--version",
            "2",
            "--apply",
            "--state-dir",
            str(tmp_path),
        )


def test_rollback_needs_a_recorded_rebind(provider: FakeProvider, tmp_path: Path) -> None:
    with pytest.raises(CliError, match="no rebind"):
        _run(provider, "voice", "rollback", NUMBER, "--state-dir", str(tmp_path))


def test_rollback_refuses_to_restore_a_floating_binding(
    provider: FakeProvider, tmp_path: Path
) -> None:
    """The number was on LATEST before: restoring that would re-create the draft-binding risk."""
    number = "+61255011140"
    provider.versions[("agent_b", 5)] = {"is_published": True}
    _run(
        provider,
        "voice",
        "rebind",
        number,
        "--agent",
        "agent_b",
        "--version",
        "5",
        "--apply",
        "--state-dir",
        str(tmp_path),
    )
    with pytest.raises(CliError, match="not a single published version"):
        _run(provider, "voice", "rollback", number, "--apply", "--state-dir", str(tmp_path))


def test_main_needs_the_key_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RETELL_API_KEY", raising=False)
    assert main(["voice", "bindings"]) == 2
