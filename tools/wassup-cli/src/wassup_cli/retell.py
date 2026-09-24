"""The few voice-provider calls the CLI needs. The API key comes from the environment only."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://api.retellai.com"


class RetellError(RuntimeError):
    """A provider call failed. The message is a status code and path, never a response body."""


class Retell:
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=15.0)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, headers=self._headers, **kwargs)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            endpoint = path.split("?", maxsplit=1)[0]
            raise RetellError(f"{method} {endpoint} -> HTTP {response.status_code}")
        return response.json() if response.content else None

    def list_phone_numbers(self) -> list[dict[str, Any]]:
        body = self._request("GET", "/list-phone-numbers")
        return [n for n in body or [] if isinstance(n, dict)]

    def get_phone_number(self, e164: str) -> dict[str, Any] | None:
        body = self._request("GET", f"/get-phone-number/{quote(e164, safe='')}")
        return body if isinstance(body, dict) else None

    def get_agent_version(self, agent_id: str, version: int) -> dict[str, Any] | None:
        body = self._request(
            "GET", f"/get-agent/{quote(agent_id, safe='')}", params={"version": version}
        )
        # Never trust a response for a different version than the one asked for.
        if not isinstance(body, dict) or body.get("version") != version:
            return None
        return body

    def get_llm_version(self, llm_id: str, version: int) -> dict[str, Any] | None:
        body = self._request(
            "GET", f"/get-retell-llm/{quote(llm_id, safe='')}", params={"version": version}
        )
        return body if isinstance(body, dict) else None

    def bind_number(self, e164: str, agent_id: str, version: int) -> dict[str, Any] | None:
        body = self._request(
            "PATCH",
            f"/update-phone-number/{quote(e164, safe='')}",
            json={
                "inbound_agents": [{"agent_id": agent_id, "agent_version": version, "weight": 1}]
            },
        )
        return body if isinstance(body, dict) else None


def binding(number: dict[str, Any]) -> list[tuple[str, int | None, float]]:
    """(agent_id, version, weight) routes of a number, from either API shape."""
    agents = number.get("inbound_agents")
    if isinstance(agents, list) and agents:
        return [
            (str(a["agent_id"]), a.get("agent_version"), float(a.get("weight", 1) or 0))
            for a in agents
            if isinstance(a, dict) and a.get("agent_id")
        ]
    if number.get("inbound_agent_id"):
        return [(str(number["inbound_agent_id"]), number.get("inbound_agent_version"), 1.0)]
    return []
