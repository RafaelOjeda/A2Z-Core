"""Integration tests for core.realtime (fakeredis pub/sub, real subscribe)."""

from __future__ import annotations

import json

import pytest

from app.core import clients, realtime

pytestmark = pytest.mark.integration


async def test_subscriber_receives_published_update() -> None:
    redis = clients.redis_client()
    pubsub = redis.pubsub()
    await pubsub.subscribe("rt:org:org-a:inbox")

    await realtime.publish_update("org-a", "org:org-a:inbox", {"type": "message.received"})

    message = None
    for _ in range(20):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if message is not None:
            break
    await pubsub.unsubscribe("rt:org:org-a:inbox")

    assert message is not None
    payload = json.loads(message["data"])
    assert payload["org_id"] == "org-a"
    assert payload["type"] == "message.received"


async def test_channels_do_not_cross_orgs() -> None:
    redis = clients.redis_client()
    pubsub_a = redis.pubsub()
    await pubsub_a.subscribe("rt:org:org-a:inbox")

    # Publish on a different org's channel — org-a's subscriber must see nothing.
    await realtime.publish_update("org-b", "org:org-b:inbox", {"type": "message.received"})

    message = await pubsub_a.get_message(ignore_subscribe_messages=True, timeout=0.3)
    await pubsub_a.unsubscribe("rt:org:org-a:inbox")

    assert message is None
