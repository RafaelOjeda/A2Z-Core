"""Integration tests for core.realtime (in-process broker, real subscribe)."""

from __future__ import annotations

import json

import pytest

from app.core import realtime

pytestmark = pytest.mark.integration


async def test_subscriber_receives_published_update() -> None:
    async with realtime.subscribe("org:org-a:inbox") as sub:
        await realtime.publish_update("org-a", "org:org-a:inbox", {"type": "message.received"})
        message = await sub.get(timeout_seconds=1.0)

    assert message is not None
    payload = json.loads(message)
    assert payload["org_id"] == "org-a"
    assert payload["type"] == "message.received"


async def test_channels_do_not_cross_orgs() -> None:
    async with realtime.subscribe("org:org-a:inbox") as sub:
        # Publish on a different org's channel — org-a's subscriber must see nothing.
        await realtime.publish_update("org-b", "org:org-b:inbox", {"type": "message.received"})
        message = await sub.get(timeout_seconds=0.3)

    assert message is None
