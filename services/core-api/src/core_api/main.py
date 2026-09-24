"""core-api: staff and dashboard API (/v1)."""

from __future__ import annotations

from fastapi import FastAPI
from wassup_core.app import create_app
from wassup_core.settings import BaseServiceSettings


class CoreApiSettings(BaseServiceSettings):
    service_name: str = "core-api"


def build_app(settings: CoreApiSettings | None = None) -> FastAPI:
    return create_app(settings or CoreApiSettings())


app = build_app()
