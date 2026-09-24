"""core-api settings (environment only)."""

from __future__ import annotations

from wassup_core.settings import BaseServiceSettings


class CoreApiSettings(BaseServiceSettings):
    service_name: str = "core-api"
    # Staff identity: Firebase Authentication ID tokens for this project.
    firebase_project_id: str = ""
    # "firebase" everywhere real; "test" issues local tokens and refuses to start outside
    # local/test environments.
    auth_mode: str = "firebase"
    # Browser origins allowed to call the API (comma-separated). Nothing else gets CORS.
    cors_origins: str = ""
    db_pool_size: int = 10
    db_statement_timeout_ms: int = 5000
    # Live events: how often each stream looks for new events, and its longest life (a stream
    # also ends when the viewer's sign-in token expires; the client reconnects with a fresh one).
    events_poll_interval_s: float = 2.0
    events_max_seconds: float = 3600.0

    @property
    def allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
