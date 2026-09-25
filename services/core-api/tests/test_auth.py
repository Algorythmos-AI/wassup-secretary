"""Firebase ID-token verification with real RS256 tokens signed by a throwaway test key."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from core_api.auth import AuthError, FirebaseVerifier, build_verifier
from core_api.settings import CoreApiSettings
from cryptography.hazmat.primitives.asymmetric import rsa
from wassup_core.settings import Environment

PROJECT = "test-project"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Key:
    def __init__(self, key: Any) -> None:
        self.key = key


class StaticKeys:
    def get_signing_key_from_jwt(self, token: str) -> _Key:
        return _Key(PRIVATE_KEY.public_key())


def _token(key: Any = PRIVATE_KEY, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": "uid-123",
        "iat": now,
        "exp": now + 3600,
        "email": "staff@example.test",
        "email_verified": True,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


def _verifier() -> FirebaseVerifier:
    return FirebaseVerifier(PROJECT, keys=StaticKeys())


def test_valid_token() -> None:
    principal = _verifier().verify(_token())
    assert (principal.uid, principal.email) == ("uid-123", "staff@example.test")


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "another-project"},
        {"iss": "https://securetoken.google.com/another-project"},
        {"exp": int(time.time()) - 3600},
        {"email_verified": False},
        {"sub": ""},
    ],
)
def test_rejected_claims(overrides: dict[str, Any]) -> None:
    with pytest.raises(AuthError):
        _verifier().verify(_token(**overrides))


def test_wrong_signing_key_rejected() -> None:
    with pytest.raises(AuthError):
        _verifier().verify(_token(key=OTHER_KEY))


def test_unsigned_token_rejected() -> None:
    forged = jwt.encode({"sub": "x", "aud": PROJECT}, key="", algorithm="none")
    with pytest.raises(AuthError):
        _verifier().verify(forged)


@pytest.mark.parametrize("env", [Environment.STAGING, Environment.PRODUCTION])
def test_test_auth_mode_is_refused_outside_local(env: Environment) -> None:
    with pytest.raises(RuntimeError):
        build_verifier(CoreApiSettings(environment=env, auth_mode="test"))
