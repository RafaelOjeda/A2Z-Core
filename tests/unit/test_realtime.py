"""Unit tests for core.realtime — verify the publish/subscribe contract."""

from __future__ import annotations

import json

import pytest

from app.core import realtime
from app.core.exceptions import RealtimeError


async def test_publish_update_shape() -> None:
    async with realtime.subscribe("org:org-a:conversations") as sub:
        await realtime.publish_update(
            "org-a", "org:org-a:conversations", {"type": "message.received"}
        )
        message = await sub.get(timeout_seconds=1.0)

    assert message is not None
    payload = json.loads(message)
    assert payload["org_id"] == "org-a"
    assert payload["type"] == "message.received"


async def test_publish_failure_raises_typed_error() -> None:
    # ``default=str`` bails us out of almost anything except a non-string
    # dict key, which json.dumps rejects outright (TypeError) regardless.
    bad_payload = {("not", "a", "string", "key"): "value"}
    with pytest.raises(RealtimeError) as exc:
        await realtime.publish_update("org-a", "chan", bad_payload)  # type: ignore[arg-type]
    assert exc.value.status_code == 502
