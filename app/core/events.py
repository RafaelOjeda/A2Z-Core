"""Events — cross-service domain events via EventBridge (CLAUDE.md §6).

Core owns the publisher; services own their subscribers (later phases). Single
custom bus ``a2z-bus``. ``source`` namespaces the producer (``a2z.core``,
``a2z.invoicing``, ...); ``detail-type`` is the dotted ``event_type``; the
``detail`` payload always carries ``org_id`` so subscribers can scope.

Fire-and-forget from the caller's view, but we await PutEvents and surface
failures rather than silently dropping. The event catalog lives in
``docs/events.md``. Performance: < 50ms.
"""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError

from app.config import settings
from app.core import clients
from app.core.exceptions import EventError
from app.core.logging import get_logger

log = get_logger("core.events")


async def publish_event(
    org_id: str,
    event_type: str,
    data: dict[str, Any],
    *,
    source: str = "a2z.core",
) -> str:
    """Publish a domain event to the A2Z bus.

    Args:
        org_id: Org the event belongs to (always injected into ``detail``).
        event_type: Dotted type, e.g. ``"member.added"`` (becomes detail-type).
        data: JSON-serializable payload.
        source: Producing namespace (defaults to ``a2z.core``).

    Returns:
        The EventBridge event id.

    Raises:
        EventError: PutEvents failed or the entry was rejected.

    Performance: < 50ms.
    """
    detail = json.dumps({**data, "org_id": org_id}, default=str)
    entry = {
        "Source": source,
        "DetailType": event_type,
        "Detail": detail,
        "EventBusName": settings().event_bus_name,
    }
    try:
        resp = await clients.run_aws(
            clients.eventbridge().put_events, Entries=[entry]
        )
    except ClientError as exc:
        raise EventError(f"Failed to publish {event_type}: {exc}") from exc

    if resp.get("FailedEntryCount", 0) > 0:
        err = resp["Entries"][0]
        raise EventError(
            f"EventBridge rejected {event_type}: "
            f"{err.get('ErrorCode')} {err.get('ErrorMessage')}"
        )

    event_id = resp["Entries"][0].get("EventId", "")
    log.info("event.published", extra={"org_id": org_id, "event_type": event_type})
    return event_id
