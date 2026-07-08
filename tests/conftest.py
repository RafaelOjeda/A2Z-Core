"""Shared test fixtures.

We run "integration" tests against **moto** (in-process AWS mocks) and **fakeredis**
so the whole suite runs anywhere — no Docker/LocalStack required in CI. The same
tests work against real LocalStack by exporting ``AWS_ENDPOINT_URL`` and a live
``REDIS_URL`` and skipping the moto fixture.

Fixtures:
  * ``aws``          — moto-mocked AWS with all Core resources provisioned.
  * ``fake_redis``   — autouse; swaps the Redis singleton for fakeredis.
  * ``make_token``   — factory producing valid test JWTs (HS256, test secret).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest

# --- Test environment must be set before app.config is imported anywhere. ---
os.environ.setdefault("A2Z_ENV", "local")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
# Empty endpoint => let moto intercept (do NOT point at LocalStack under moto).
os.environ["AWS_ENDPOINT_URL"] = ""
os.environ.setdefault("TEST_JWT_SECRET", "test-secret-key-for-suite")


@pytest.fixture
def aws() -> Iterator[None]:
    """Provision all Core (+ Omni-Channel) AWS resources inside a moto mock."""
    from moto import mock_aws

    from app.core import clients
    from app.services.omnichannel import queues as omnichannel_queues
    from scripts.create_local_resources import main as provision

    with mock_aws():
        clients.reset_clients()
        omnichannel_queues.reset_cache()
        provision()
        yield
    clients.reset_clients()
    omnichannel_queues.reset_cache()


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Replace the Redis singleton with an isolated fakeredis per test."""
    import fakeredis.aioredis as fakeaioredis

    from app.core import clients

    server_client = fakeaioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(clients, "redis_client", lambda: server_client)
    yield


@pytest.fixture
def make_token() -> Callable[..., str]:
    """Return a factory that mints valid test JWTs (HS256, test secret)."""
    from app.core import auth

    def _make(sub: str = "auth0|test-user", email: str = "test@example.com") -> str:
        return auth.create_test_token(sub, email)

    return _make
