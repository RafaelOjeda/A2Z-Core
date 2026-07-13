"""Realtime — fan-out to connected clients (root CLAUDE.md §6.2 gap).

MVP transport (single-EC2 deployment, app/services/omnichannel/CLAUDE.md
§12): Redis pub/sub. The service-owned API process — not Core — relays
published messages to browsers over SSE; Core's job stops at the publish.
When a service distributes onto its own infrastructure, this function's
*contract* stays the same and only the transport underneath swaps to an
AppSync GraphQL mutation — callers never change (§6.2 of that same doc).

Fire-and-forget from the caller's perspective: publish failures are raised,
not swallowed, but a slow or absent subscriber never blocks the publisher —
Redis ``PUBLISH`` does not wait for a receiver.
"""

from __future__ import annotations

import json
from typing import Any

from redis.exceptions import RedisError

from app.core import clients
from app.core.exceptions import RealtimeError
from app.core.logging import get_logger

log = get_logger("core.realtime")


async def publish_update(org_id: str, channel: str, payload: dict[str, Any]) -> None:
    """Push a real-time update to connected clients.

    Args:
        org_id: Org the update belongs to. Callers should also scope
            ``channel`` to the org (e.g. ``f"org:{org_id}:conversations"``)
            so a subscriber can never cross an org boundary by construction;
            this is injected into the payload as a defense-in-depth check.
        channel: Logical fan-out channel name.
        payload: JSON-serializable update body.

    Raises:
        RealtimeError: The publish itself failed (Redis unreachable/misconfigured).

    Performance: < 100ms.
    """
    redis = clients.redis_client()
    message = json.dumps({**payload, "org_id": org_id}, default=str)
    try:
        await redis.publish(f"rt:{channel}", message)
    except RedisError as exc:
        log.error("realtime.publish_failed", extra={"org_id": org_id, "channel": channel})
        raise RealtimeError(f"Failed to publish update on {channel}: {exc}") from exc

    log.info("realtime.published", extra={"org_id": org_id, "channel": channel})
