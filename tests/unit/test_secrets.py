"""Unit tests for core.secrets — cache hit/miss + not-found mapping."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from app.core import clients, secrets
from app.core.exceptions import SecretNotFoundError, SecretsError


def _fake_secretsmanager(secret_string: str | None = None, *, not_found: bool = False) -> Mock:
    sm = Mock()
    if not_found:
        sm.get_secret_value = Mock(
            side_effect=ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "nope"}},
                "GetSecretValue",
            )
        )
    else:
        sm.get_secret_value = Mock(return_value={"SecretString": secret_string})
    return sm


async def test_cache_miss_fetches_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    value = {"access_token": "tok-123"}
    sm = _fake_secretsmanager(json.dumps(value))
    monkeypatch.setattr(clients, "secretsmanager", lambda: sm)

    result = await secrets.get_secret("org-a", "omnichannel", "whatsapp")

    assert result == value
    sm.get_secret_value.assert_called_once_with(SecretId="a2z/org-a/omnichannel/whatsapp")

    # Second read hits the cache — no further AWS call.
    result2 = await secrets.get_secret("org-a", "omnichannel", "whatsapp")
    assert result2 == value
    sm.get_secret_value.assert_called_once()


async def test_not_found_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sm = _fake_secretsmanager(not_found=True)
    monkeypatch.setattr(clients, "secretsmanager", lambda: sm)

    with pytest.raises(SecretNotFoundError) as exc:
        await secrets.get_secret("org-a", "omnichannel", "missing")
    assert exc.value.status_code == 404


async def test_cross_org_secrets_are_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    sm = Mock()
    sm.get_secret_value = Mock(
        side_effect=[
            {"SecretString": json.dumps({"token": "org-a-token"})},
            {"SecretString": json.dumps({"token": "org-b-token"})},
        ]
    )
    monkeypatch.setattr(clients, "secretsmanager", lambda: sm)

    a = await secrets.get_secret("org-a", "omnichannel", "whatsapp")
    b = await secrets.get_secret("org-b", "omnichannel", "whatsapp")

    assert a == {"token": "org-a-token"}
    assert b == {"token": "org-b-token"}
    assert sm.get_secret_value.call_count == 2
    ids = [call.kwargs["SecretId"] for call in sm.get_secret_value.call_args_list]
    assert ids == ["a2z/org-a/omnichannel/whatsapp", "a2z/org-b/omnichannel/whatsapp"]


# --- put_secret (self-service write path) ---


async def test_put_secret_updates_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    sm = Mock()
    sm.put_secret_value = Mock(return_value={})
    monkeypatch.setattr(clients, "secretsmanager", lambda: sm)

    await secrets.put_secret("org-a", "omnichannel", "conn-1", {"access_token": "tok"})

    sm.put_secret_value.assert_called_once_with(
        SecretId="a2z/org-a/omnichannel/conn-1", SecretString=json.dumps({"access_token": "tok"})
    )
    sm.create_secret.assert_not_called()


async def test_put_secret_creates_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    sm = Mock()
    sm.put_secret_value = Mock(
        side_effect=ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "nope"}}, "PutSecretValue"
        )
    )
    sm.create_secret = Mock(return_value={})
    monkeypatch.setattr(clients, "secretsmanager", lambda: sm)

    await secrets.put_secret("org-a", "omnichannel", "conn-1", {"access_token": "tok"})

    sm.create_secret.assert_called_once_with(
        Name="a2z/org-a/omnichannel/conn-1", SecretString=json.dumps({"access_token": "tok"})
    )


async def test_put_secret_wraps_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sm = Mock()
    sm.put_secret_value = Mock(
        side_effect=ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutSecretValue"
        )
    )
    monkeypatch.setattr(clients, "secretsmanager", lambda: sm)

    with pytest.raises(SecretsError):
        await secrets.put_secret("org-a", "omnichannel", "conn-1", {"access_token": "tok"})


async def test_put_secret_invalidates_cache_so_next_read_is_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sm = Mock()
    sm.get_secret_value = Mock(
        side_effect=[
            {"SecretString": json.dumps({"access_token": "old"})},
            {"SecretString": json.dumps({"access_token": "new"})},
        ]
    )
    sm.put_secret_value = Mock(return_value={})
    monkeypatch.setattr(clients, "secretsmanager", lambda: sm)

    first = await secrets.get_secret("org-a", "omnichannel", "conn-1")  # primes the cache
    assert first == {"access_token": "old"}

    await secrets.put_secret("org-a", "omnichannel", "conn-1", {"access_token": "new"})

    second = await secrets.get_secret("org-a", "omnichannel", "conn-1")
    assert second == {"access_token": "new"}
    assert sm.get_secret_value.call_count == 2
