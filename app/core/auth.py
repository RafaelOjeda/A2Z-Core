"""Auth — Cognito JWT validation and a test-token factory (Design §2.1).

Validation is intentionally **synchronous**: the design calls it inline in request
handlers without ``await``, and once signing keys are cached the work is pure CPU
(signature + claim checks). We cache the Cognito JWKS **in-process** with a 24h TTL
rather than in Redis — Cognito rotates keys rarely, an in-memory cache keeps the
hot path free of network I/O, and it avoids a second (sync) Redis client. A
Redis-backed cross-instance cache is a deferred optimization (CLAUDE.md §16: when
ambiguous, document the decision).

Two token shapes are accepted:
  * **Cognito RS256** — verified against the pool's JWKS, issuer-checked. The only
    shape accepted in prod.
  * **Test HS256** — minted by :func:`create_test_token`, signed with
    ``TEST_JWT_SECRET``. Rejected whenever ``A2Z_ENV == "prod"``.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError

from app.config import settings
from app.core.exceptions import InvalidTokenError, MissingTokenError
from app.core.logging import get_logger

log = get_logger("core.auth")

_JWKS_TTL_SECONDS = 24 * 3600
_TEST_ALGS = ["HS256"]
_COGNITO_ALGS = ["RS256"]

# In-process JWKS cache: (fetched_at_epoch, keys_by_kid).
_jwks_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


def _fetch_jwks() -> dict[str, dict[str, Any]]:
    """Fetch and index the Cognito JWKS by ``kid`` (24h in-process cache)."""
    global _jwks_cache
    now = time.time()
    if _jwks_cache is not None and now - _jwks_cache[0] < _JWKS_TTL_SECONDS:
        return _jwks_cache[1]

    url = f"{settings().cognito_issuer}/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 (trusted AWS URL)
        raw = json.loads(resp.read())
    keys = {k["kid"]: k for k in raw.get("keys", [])}
    _jwks_cache = (now, keys)
    log.info("auth.jwks.refreshed", extra={"key_count": len(keys)})
    return keys


def _validate_cognito(token: str) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise InvalidTokenError("Malformed token header") from exc

    kid = header.get("kid")
    keys = _fetch_jwks()
    key = keys.get(kid) if kid else None
    if key is None:
        # Possible key rotation — drop cache and retry once.
        global _jwks_cache
        _jwks_cache = None
        key = _fetch_jwks().get(kid) if kid else None
    if key is None:
        raise InvalidTokenError("Signing key not found for token")

    try:
        return dict(
            jwt.decode(
                token,
                key,
                algorithms=_COGNITO_ALGS,
                issuer=settings().cognito_issuer,
                options={"verify_aud": False},  # app_client_id checked separately
            )
        )
    except ExpiredSignatureError as exc:
        raise InvalidTokenError("Token expired") from exc
    except JWTError as exc:
        raise InvalidTokenError("Token validation failed") from exc


def _validate_test_token(token: str) -> dict[str, Any]:
    if settings().is_prod:
        raise InvalidTokenError("Test tokens are not accepted in production")
    try:
        return dict(
            jwt.decode(
                token,
                settings().test_jwt_secret,
                algorithms=_TEST_ALGS,
                options={"verify_aud": False},
            )
        )
    except ExpiredSignatureError as exc:
        raise InvalidTokenError("Token expired") from exc
    except JWTError as exc:
        raise InvalidTokenError("Token validation failed") from exc


def validate_jwt(token: str) -> dict[str, Any]:
    """Validate a JWT and return its claims.

    Args:
        token: Bearer token value (without the ``Bearer `` prefix).

    Returns:
        Claims dict including ``sub``, ``email``, ``email_verified`` and
        ``cognito:username``.

    Raises:
        MissingTokenError: Token is empty/None.
        InvalidTokenError: Token is malformed, expired, or has a bad signature.

    Performance:
        < 5ms once JWKS is cached (pure CPU signature verification).
    """
    if not token:
        raise MissingTokenError("No token provided")

    try:
        alg = jwt.get_unverified_header(token).get("alg")
    except JWTError as exc:
        raise InvalidTokenError("Malformed token header") from exc

    if alg in _TEST_ALGS:
        return _validate_test_token(token)
    return _validate_cognito(token)


def get_current_user_from_request(request: Any) -> dict[str, Any]:
    """Extract and validate the bearer token from a request's Authorization header.

    Args:
        request: Any object exposing a mapping-like ``.headers`` (e.g. a FastAPI/
            Starlette ``Request``).

    Returns:
        Validated claims dict (see :func:`validate_jwt`).

    Raises:
        MissingTokenError: No ``Authorization`` header present.
        InvalidTokenError: Header malformed or token invalid.
    """
    header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not header:
        raise MissingTokenError("No Authorization header")
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise InvalidTokenError("Authorization header must be 'Bearer <token>'")
    return validate_jwt(parts[1].strip())


def create_test_token(
    sub: str,
    email: str,
    *,
    expires_in: int = 3600,
    email_verified: bool = True,
) -> str:
    """Mint a valid HS256 test JWT (test/dev only).

    Args:
        sub: Cognito-style user id placed in the ``sub`` claim.
        email: Email claim.
        expires_in: Seconds until expiry (default 1h).
        email_verified: Value for the ``email_verified`` claim.

    Returns:
        Signed JWT string.

    Raises:
        InvalidTokenError: Called while ``A2Z_ENV == "prod"``.
    """
    if settings().is_prod:
        raise InvalidTokenError("Refusing to mint test tokens in production")
    now = int(time.time())
    claims = {
        "sub": sub,
        "email": email,
        "email_verified": email_verified,
        "cognito:username": email,
        "token_use": "id",
        "iat": now,
        "exp": now + expires_in,
    }
    return str(jwt.encode(claims, settings().test_jwt_secret, algorithm="HS256"))
