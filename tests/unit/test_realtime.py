"""Unit tests for core.realtime — verify the publish call shape."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError

from app.core import clients, realtime
from app.core.exceptions import RealtimeError


async def test_publish_update_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = AsyncMock()
    monkeypatch.setattr(clients, "redis_client", lambda: redis)

    await realtime.publish_update("org-a", "org:org-a:conversations", {"type": "message.received"})

    redis.publish.assert_called_once()
    channel, message = redis.publish.call_args.args
    assert channel == "rt:org:org-a:conversations"
    payload = json.loads(message)
    assert payload["org_id"] == "org-a"
    assert payload["type"] == "message.received"


async def test_publish_failure_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = AsyncMock()
    redis.publish.side_effect = RedisError("connection refused")
    monkeypatch.setattr(clients, "redis_client", lambda: redis)

    with pytest.raises(RealtimeError) as exc:
        await realtime.publish_update("org-a", "chan", {})
    assert exc.value.status_code == 502
