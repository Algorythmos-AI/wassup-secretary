"""ops-worker: scheduled and background jobs. Exposes /health for the platform and monitors."""

from __future__ import annotations

from fastapi import FastAPI
from wassup_core.app import create_app
from wassup_core.settings import BaseServiceSettings


class OpsWorkerSettings(BaseServiceSettings):
    service_name: str = "ops-worker"


def build_app(settings: OpsWorkerSettings | None = None) -> FastAPI:
    return create_app(settings or OpsWorkerSettings())


app = build_app()
