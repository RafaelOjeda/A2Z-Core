"""Shared test fixtures.

We run "integration" tests against **moto** (in-process AWS mocks) so most of
the suite runs anywhere — no Docker/LocalStack required in CI. The same
tests work against real LocalStack by exporting ``AWS_ENDPOINT_URL`` and
skipping the moto fixture. Omni-Channel/Invoicing's Postgres-backed tests are
the one exception requiring a real Postgres (see their own conftest.py).

Fixtures:
  * ``aws``               — moto-mocked AWS with all Core resources provisioned.
  * ``_reset_core_state`` — autouse; clears in-process module-global state
    (realtime subscribers, rate-limit windows, TTL caches) between tests so
    they don't contaminate each other via shared keys -- the equivalent of
    each test getting a fresh backend, now that these live in-process instead
    of in a per-test-isolated Redis.
  * ``make_token``        — factory producing valid test JWTs (HS256, test secret).
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
        omnichannel_queues.reset_queue_url_cache()
        provision()
        yield
    clients.reset_clients()
    omnichannel_queues.reset_queue_url_cache()


@pytest.fixture(autouse=True)
def _reset_core_state() -> None:
    """Clear in-process module-global state that would otherwise leak between
    tests -- these modules hold no per-test isolation of their own, so
    without this a value set in one test (a rate-limit window, a cached
    secret, a realtime subscriber) would still be there in the next."""
    from app.core import cache, rate_limit, realtime

    realtime.reset()
    rate_limit.reset()
    cache.clear_all()


@pytest.fixture
def make_token() -> Callable[..., str]:
    """Return a factory that mints valid test JWTs (HS256, test secret)."""
    from app.core import auth

    def _make(sub: str = "auth0|test-user", email: str = "test@example.com") -> str:
        return auth.create_test_token(sub, email)

    return _make
