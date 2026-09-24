"""Minimal Retell REST client used by ops-worker (read calls, place line-check calls)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

BASE_URL = "https://api.retellai.com"


class RetellApi(Protocol):
    async def list_calls(self, agent_ids: list[str], limit: int) -> list[dict[str, Any]]: ...

    async def create_phone_call(
        self, from_number: str, to_number: str, agent_id: str, agent_version: int | None
    ) -> str | None: ...


@dataclass
class RetellClient:
    api_key: str
    timeout_s: float = 8.0

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def list_calls(self, agent_ids: list[str], limit: int) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=self.timeout_s) as client:
            response = await client.post(
                "/v2/list-calls",
                headers=self._headers(),
                json={
                    "limit": limit,
                    "sort_order": "descending",
                    "filter_criteria": {"agent_id": agent_ids},
                },
            )
        response.raise_for_status()
        calls = response.json()
        return calls if isinstance(calls, list) else []

    async def create_phone_call(
        self, from_number: str, to_number: str, agent_id: str, agent_version: int | None
    ) -> str | None:
        body: dict[str, Any] = {
            "from_number": from_number,
            "to_number": to_number,
            "override_agent_id": agent_id,
            "metadata": {"wassup_canary": True},
        }
        if agent_version is not None:
            body["override_agent_version"] = agent_version
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=self.timeout_s) as client:
            response = await client.post(
                "/v2/create-phone-call", headers=self._headers(), json=body
            )
        response.raise_for_status()
        call_id = response.json().get("call_id")
        return str(call_id) if call_id else None
