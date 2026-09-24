"""voice-gateway: the only service the voice provider calls (signed webhooks and tool calls)."""

from __future__ import annotations

from fastapi import FastAPI
from wassup_core.app import create_app
from wassup_core.settings import BaseServiceSettings

# Sized from production traffic: a full call_analyzed payload was p50 17 KB, p99 87 KB,
# max 154 KB (Sept 2026). Limits leave >30x headroom so a long call is never dropped.
WEBHOOK_BODY_LIMIT = 5 * 1024 * 1024
TOOL_BODY_LIMIT = 2 * 1024 * 1024


class VoiceGatewaySettings(BaseServiceSettings):
    service_name: str = "voice-gateway"


def build_app(settings: VoiceGatewaySettings | None = None) -> FastAPI:
    settings = settings or VoiceGatewaySettings()
    return create_app(
        settings,
        body_limits={"/v1/retell/webhook": WEBHOOK_BODY_LIMIT, "/v1/retell/tools": TOOL_BODY_LIMIT},
    )


app = build_app()
