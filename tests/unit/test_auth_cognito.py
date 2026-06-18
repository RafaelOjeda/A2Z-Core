"""Unit tests for the Cognito RS256 validation path (mocked JWKS)."""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from jose.backends.cryptography_backend import CryptographyRSAKey

from app.config import settings
from app.core import auth
from app.core.exceptions import InvalidTokenError

_KID = "test-kid-1"


def _rsa_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture
def rsa_setup(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Generate an RSA key, expose its public JWK via a mocked _fetch_jwks."""
    pem = _rsa_pem()
    public_jwk = CryptographyRSAKey(pem, "RS256").public_key().to_dict()
    public_jwk["kid"] = _KID
    monkeypatch.setattr(auth, "_fetch_jwks", lambda: {_KID: public_jwk})
    # Give the issuer a concrete value so the iss check has something to match.
    monkeypatch.setattr(settings(), "cognito_user_pool_id", "us-east-1_pool")
    return pem


def _cognito_token(pem: bytes, *, iss: str, exp_delta: int = 300) -> str:
    now = int(time.time())
    claims = {
        "sub": "cognito-sub-1",
        "email": "user@cognito.com",
        "email_verified": True,
        "cognito:username": "user@cognito.com",
        "iss": iss,
        "iat": now,
        "exp": now + exp_delta,
    }
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": _KID})


def test_valid_cognito_token(rsa_setup: bytes) -> None:
    token = _cognito_token(rsa_setup, iss=settings().cognito_issuer)
    claims = auth.validate_jwt(token)
    assert claims["sub"] == "cognito-sub-1"
    assert claims["email"] == "user@cognito.com"


def test_expired_cognito_token(rsa_setup: bytes) -> None:
    token = _cognito_token(rsa_setup, iss=settings().cognito_issuer, exp_delta=-10)
    with pytest.raises(InvalidTokenError):
        auth.validate_jwt(token)


def test_wrong_issuer_rejected(rsa_setup: bytes) -> None:
    token = _cognito_token(rsa_setup, iss="https://evil.example.com/pool")
    with pytest.raises(InvalidTokenError):
        auth.validate_jwt(token)


def test_unknown_kid_rejected(rsa_setup: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    token = _cognito_token(rsa_setup, iss=settings().cognito_issuer)
    # Drop the key so kid lookup fails even after a cache-refresh retry.
    monkeypatch.setattr(auth, "_fetch_jwks", lambda: {})
    with pytest.raises(InvalidTokenError):
        auth.validate_jwt(token)


def test_malformed_token_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        auth.validate_jwt("not-a-jwt")


def test_fetch_jwks_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """_fetch_jwks hits the network once, then serves from the in-process cache."""
    import io
    import json

    calls = {"n": 0}
    payload = json.dumps({"keys": [{"kid": "k1", "kty": "RSA"}]}).encode()

    class _Resp(io.BytesIO):
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    def _fake_urlopen(url: str, timeout: int = 5) -> _Resp:
        calls["n"] += 1
        return _Resp(payload)

    monkeypatch.setattr(auth, "_jwks_cache", None)
    monkeypatch.setattr(auth.urllib.request, "urlopen", _fake_urlopen)

    first = auth._fetch_jwks()
    second = auth._fetch_jwks()
    assert "k1" in first and first == second
    assert calls["n"] == 1  # second call served from cache

