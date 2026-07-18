"""Integration tests for core.secrets (moto Secrets Manager + fakeredis)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import pytest

from app.core import clients, secrets
from app.core.exceptions import SecretNotFoundError

pytestmark = pytest.mark.integration


async def _seed(org_id: str, service_type: str, key: str, value: dict[str, Any]) -> None:
    await clients.run_aws(
        clients.secretsmanager().create_secret,
        Name=f"a2z/{org_id}/{service_type}/{key}",
        SecretString=json.dumps(value),
    )


async def test_get_secret_round_trip(aws: None) -> None:
    await _seed("org-a", "omnichannel", "whatsapp", {"access_token": "tok-abc"})
    result = await secrets.get_secret("org-a", "omnichannel", "whatsapp")
    assert result == {"access_token": "tok-abc"}


async def test_not_found(aws: None) -> None:
    with pytest.raises(SecretNotFoundError):
        await secrets.get_secret("org-a", "omnichannel", "does-not-exist")


async def test_cross_org_isolation(aws: None) -> None:
    await _seed("org-a", "omnichannel", "whatsapp", {"token": "a"})
    await _seed("org-b", "omnichannel", "whatsapp", {"token": "b"})

    a = await secrets.get_secret("org-a", "omnichannel", "whatsapp")
    b = await secrets.get_secret("org-b", "omnichannel", "whatsapp")

    assert a == {"token": "a"}
    assert b == {"token": "b"}


async def test_cache_hit_skips_second_aws_call(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed("org-a", "omnichannel", "whatsapp", {"token": "a"})
    await secrets.get_secret("org-a", "omnichannel", "whatsapp")  # primes the cache

    # Swap in a client that fails any call — a cache hit must never reach it.
    broken = Mock()
    broken.get_secret_value = Mock(side_effect=AssertionError("should not hit AWS on a cache hit"))
    monkeypatch.setattr(clients, "secretsmanager", lambda: broken)

    result = await secrets.get_secret("org-a", "omnichannel", "whatsapp")
    assert result == {"token": "a"}


async def test_put_secret_round_trip_against_moto(aws: None) -> None:
    """The self-service write path: no pre-seeded secret, put_secret creates it."""
    await secrets.put_secret("org-a", "omnichannel", "conn-1", {"access_token": "tok-abc"})

    result = await secrets.get_secret("org-a", "omnichannel", "conn-1")
    assert result == {"access_token": "tok-abc"}


async def test_put_secret_updates_existing_against_moto(aws: None) -> None:
    await _seed("org-a", "omnichannel", "conn-1", {"access_token": "old"})

    await secrets.put_secret("org-a", "omnichannel", "conn-1", {"access_token": "new"})

    result = await secrets.get_secret("org-a", "omnichannel", "conn-1")
    assert result == {"access_token": "new"}


async def test_put_secret_cross_org_isolation(aws: None) -> None:
    await secrets.put_secret("org-a", "omnichannel", "conn-1", {"token": "a"})
    await secrets.put_secret("org-b", "omnichannel", "conn-1", {"token": "b"})

    a = await secrets.get_secret("org-a", "omnichannel", "conn-1")
    b = await secrets.get_secret("org-b", "omnichannel", "conn-1")
    assert a == {"token": "a"}
    assert b == {"token": "b"}
