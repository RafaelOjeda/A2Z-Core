"""Unit tests for core.auth — test-token round-trip, expiry, tampering, prod guard."""

from __future__ import annotations

import time

import pytest

from app.config import settings
from app.core import auth
from app.core.exceptions import InvalidTokenError, MissingTokenError


def test_create_and_validate_round_trip() -> None:
    token = auth.create_test_token("auth0|abc", "abc@example.com")
    claims = auth.validate_jwt(token)
    assert claims["sub"] == "auth0|abc"
    assert claims["email"] == "abc@example.com"
    assert claims["email_verified"] is True
    assert claims["cognito:username"] == "abc@example.com"


def test_empty_token_raises_missing() -> None:
    with pytest.raises(MissingTokenError):
        auth.validate_jwt("")


def test_expired_token_rejected() -> None:
    token = auth.create_test_token("auth0|x", "x@example.com", expires_in=-1)
    with pytest.raises(InvalidTokenError):
        auth.validate_jwt(token)


def test_tampered_token_rejected() -> None:
    token = auth.create_test_token("auth0|x", "x@example.com")
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    with pytest.raises(InvalidTokenError):
        auth.validate_jwt(tampered)


def test_wrong_secret_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from jose import jwt

    bad = jwt.encode(
        {"sub": "s", "email": "e", "exp": int(time.time()) + 60},
        "a-different-secret",
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        auth.validate_jwt(bad)


def test_prod_blocks_test_token_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings(), "env", "prod")
    with pytest.raises(InvalidTokenError):
        auth.create_test_token("s", "e@example.com")


def test_prod_rejects_hs256_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mint while non-prod, then validate as if in prod -> must be rejected.
    token = auth.create_test_token("s", "e@example.com")
    monkeypatch.setattr(settings(), "env", "prod")
    with pytest.raises(InvalidTokenError):
        auth.validate_jwt(token)


def test_get_current_user_from_request() -> None:
    token = auth.create_test_token("auth0|req", "req@example.com")

    class _Req:
        headers = {"authorization": f"Bearer {token}"}

    claims = auth.get_current_user_from_request(_Req())
    assert claims["sub"] == "auth0|req"


def test_missing_header_raises() -> None:
    class _Req:
        headers: dict[str, str] = {}

    with pytest.raises(MissingTokenError):
        auth.get_current_user_from_request(_Req())


def test_malformed_authorization_header() -> None:
    class _Req:
        headers = {"authorization": "Token xyz"}

    with pytest.raises(InvalidTokenError):
        auth.get_current_user_from_request(_Req())
