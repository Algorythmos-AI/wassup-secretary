"""Service settings, read from the environment (never from files in git)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class BaseServiceSettings(BaseSettings):
    """Settings shared by every service. Services subclass this and add their own fields."""

    model_config = SettingsConfigDict(env_prefix="WASSUP_", extra="ignore", frozen=True)

    service_name: str = "wassup"
    # Fails closed: a deployment that forgets WASSUP_ENVIRONMENT gets production behaviour (no
    # API docs, no test sign-in), never the permissive local defaults.
    environment: Environment = Environment.PRODUCTION
    # Build identity, injected by the image build. /health reports it so a deploy can be
    # verified against the exact source tree it was built from.
    version: str = "0.0.0-dev"
    git_tree: str = "unknown"
    log_level: str = "INFO"
    database_url: SecretStr | None = Field(default=None)

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def expose_api_docs(self) -> bool:
        """Interactive docs and the OpenAPI schema are never served in production."""
        return self.environment in (Environment.LOCAL, Environment.TEST)
