"""Outbound alert channels. Email via Resend. Subjects never carry personal data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


class EmailSender(Protocol):
    async def send(self, to: list[str], subject: str, body: str) -> None: ...


@dataclass
class ResendEmailSender:
    api_key: str
    sender: str
    timeout_s: float = 5.0

    async def send(self, to: list[str], subject: str, body: str) -> None:
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"from": self.sender, "to": to, "subject": subject, "text": body},
            )
        if response.status_code >= 300:
            raise RuntimeError(f"resend_http_{response.status_code}")


class NotConfiguredSender:
    """Used when no email provider is configured: delivery fails loudly and is retried."""

    async def send(self, to: list[str], subject: str, body: str) -> None:
        raise RuntimeError("email_not_configured")
