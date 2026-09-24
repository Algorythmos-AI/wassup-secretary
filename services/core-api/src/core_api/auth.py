"""Staff authentication: Firebase ID tokens, verified locally against Google's public keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from wassup_core.settings import Environment

from core_api.settings import CoreApiSettings

FIREBASE_JWKS_URL = (
    "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
)


class AuthError(Exception):
    """Token missing, malformed, expired, for another project, or email not verified."""


@dataclass(frozen=True)
class Principal:
    uid: str
    email: str
    # When the sign-in token stops being valid (Unix seconds); long-lived streams end by then.
    expires_at: float | None = None


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...


class SigningKeyResolver(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class FirebaseVerifier:
    """Checks signature (RS256, Google's rotating keys), audience, issuer, expiry and that the
    email is verified. Keys are fetched and cached by PyJWKClient."""

    def __init__(
        self, project_id: str, keys: SigningKeyResolver | None = None, leeway_s: int = 30
    ) -> None:
        if not project_id:
            raise ValueError("firebase_project_id is required")
        self.project_id = project_id
        self.keys = keys or jwt.PyJWKClient(FIREBASE_JWKS_URL, cache_keys=True, lifespan=3600)
        self.leeway_s = leeway_s

    def verify(self, token: str) -> Principal:
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.project_id,
                issuer=f"https://securetoken.google.com/{self.project_id}",
                leeway=self.leeway_s,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            raise AuthError(type(exc).__name__) from exc
        if not claims.get("sub") or not claims.get("email_verified") or not claims.get("email"):
            raise AuthError("unverified_or_incomplete_identity")
        return Principal(
            uid=str(claims["sub"]), email=str(claims["email"]), expires_at=float(claims["exp"])
        )


class TestModeVerifier:
    """Local/test only. Token format: ``test:<uid>:<email>``."""

    def verify(self, token: str) -> Principal:
        parts = token.split(":", 2)
        if len(parts) != 3 or parts[0] != "test" or not parts[1]:
            raise AuthError("bad_test_token")
        return Principal(uid=parts[1], email=parts[2])


def build_verifier(settings: CoreApiSettings) -> TokenVerifier:
    if settings.auth_mode == "test":
        if settings.environment not in (Environment.LOCAL, Environment.TEST):
            raise RuntimeError("auth_mode=test is refused outside local and test environments")
        return TestModeVerifier()
    return FirebaseVerifier(settings.firebase_project_id)
