"""voice-gateway settings (environment only)."""

from __future__ import annotations

from pydantic import SecretStr
from wassup_core.settings import BaseServiceSettings


class VoiceGatewaySettings(BaseServiceSettings):
    service_name: str = "voice-gateway"
    retell_api_key: SecretStr | None = None
    # Set only during a key rotation, so calls signed with the old key keep verifying.
    retell_api_key_previous: SecretStr | None = None
    # Our own AI line numbers (comma-separated E.164). A call FROM one of these is a synthetic
    # line check and is never stored as a patient call.
    ai_line_numbers: str = ""
    db_pool_size: int = 5
    db_pool_timeout_s: float = 0.3
    db_statement_timeout_ms: int = 1000
    # Hard budget for a voice tool call; past it the caller hears the tool's fallback line.
    tool_budget_ms: int = 1500

    @property
    def retell_keys(self) -> list[str]:
        keys = [
            k.get_secret_value() for k in (self.retell_api_key, self.retell_api_key_previous) if k
        ]
        return [k for i, k in enumerate(keys) if k and k not in keys[:i]]

    @property
    def ai_lines(self) -> frozenset[str]:
        return frozenset(n.strip() for n in self.ai_line_numbers.split(",") if n.strip())
