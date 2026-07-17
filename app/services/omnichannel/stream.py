"""Real-time inbox relay -- the browser-facing subscribe side of §5.4.

`core.realtime.publish_update` (called from the worker and routing) pushes
onto Redis pub/sub; this module is the *other half* at MVP: the service-owned
API process subscribes and relays to browsers as Server-Sent Events. Per the
plan, "Core's job stops at the publish" -- the SSE relay is deliberately
**not** in Core, because at the distribution phase it disappears entirely
(browsers connect straight to AppSync GraphQL subscriptions; there is no
relay to keep). So this is MVP-only glue, sized accordingly: no new
dependency, just a plain async generator behind a FastAPI streaming response.

The one coupling to Core is the ``rt:{channel}`` key convention, which
``core.realtime.publish_update`` owns. It's duplicated in ``_channel_key``
here rather than exported from Core (that would be a Core change for a
throwaway MVP artifact); ``test_stream.py`` locks the two together with a
round-trip test -- publish via ``core.realtime``, receive here -- so any
drift in Core's prefix fails loudly.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable

from app.core import clients
from app.core.logging import get_logger

log = get_logger("omnichannel.stream")

# Comment-line heartbeat cadence. Keeps the connection alive through proxies
# and gives the loop a regular tick to notice a disconnected client / an
# elapsed lifetime even when no messages are flowing.
_HEARTBEAT_SECONDS = 15.0
# Server-side safety cap on a single stream. The browser closes idle/
# backgrounded tabs after ~5 min and reconnects on focus (§5.4, client-side);
# this bounds server resource use for anything that doesn't, and -- because
# membership is re-checked on every (re)connect -- caps how long a revoked
# member's stream can outlive the revocation.
_MAX_LIFETIME_SECONDS = 300.0


def _channel_key(channel: str) -> str:
    """Redis pub/sub key for a logical channel. MUST match core.realtime."""
    return f"rt:{channel}"


def _frame_data(data: str) -> str:
    """One SSE ``data:`` event. ``data`` is the already-JSON payload string."""
    return f"data: {data}\n\n"


def _frame_comment(text: str) -> str:
    """An SSE comment line (``:`` prefix) -- ignored by clients, used as heartbeat."""
    return f": {text}\n\n"


def _channels_for(org_id: str, user_id: str) -> list[str]:
    """The two channels an agent's inbox listens on (§5.6 fan-out targets).

    - org-wide inbox updates (new message, assignment change, delivery tick)
    - this user's own notifications (e.g. a conversation assigned to them)
    """
    return [
        _channel_key(f"org:{org_id}:conversations"),
        _channel_key(f"user:{user_id}:notifications"),
    ]


async def stream_events(
    org_id: str,
    user_id: str,
    *,
    heartbeat_seconds: float = _HEARTBEAT_SECONDS,
    max_lifetime_seconds: float | None = _MAX_LIFETIME_SECONDS,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[str]:
    """Yield SSE frames for one agent's inbox until disconnect/lifetime cap.

    Subscribes to the org + user channels, emits an initial ``connected``
    comment, then relays each published update as a ``data:`` frame and a
    ``keepalive`` comment on every idle ``heartbeat_seconds`` tick. The
    subscription is always torn down (unsubscribe + close) on exit, whether
    the client disconnected, the lifetime elapsed, or the generator was
    cancelled by the streaming response.

    Args:
        org_id: Org whose inbox this stream serves (already membership-checked
            by the caller -- see the router).
        user_id: The connected agent, for their personal notification channel.
        heartbeat_seconds: Idle cadence for keepalive comments.
        max_lifetime_seconds: Hard cap on stream duration; ``None`` = unbounded.
        is_disconnected: Optional async predicate (the request's
            ``is_disconnected``) checked each tick for prompt teardown.
        clock: Monotonic clock injection point for tests.

    Performance: relay latency is one Redis pub/sub hop, < 100ms end to end.
    """
    channels = _channels_for(org_id, user_id)
    redis = clients.redis_client()
    pubsub = redis.pubsub()
    await pubsub.subscribe(*channels)
    started = clock()
    log.info("omnichannel.stream.opened", extra={"org_id": org_id, "user_id": user_id})
    try:
        yield _frame_comment("connected")
        while True:
            if max_lifetime_seconds is not None and clock() - started >= max_lifetime_seconds:
                log.info("omnichannel.stream.lifetime", extra={"org_id": org_id})
                return
            if is_disconnected is not None and await is_disconnected():
                return
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=heartbeat_seconds
            )
            if message is None:
                yield _frame_comment("keepalive")
                continue
            data = message["data"]
            yield _frame_data(data if isinstance(data, str) else data.decode("utf-8"))
    finally:
        # Best-effort teardown -- never let cleanup errors mask the real exit
        # (client disconnect surfaces here as CancelledError/GeneratorExit).
        try:
            await pubsub.unsubscribe(*channels)
            # aclose() is the non-deprecated name (redis >= 5.0.1); the pinned
            # types-redis stub still only knows the old close(), hence the ignore.
            await pubsub.aclose()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 -- teardown must not raise
            log.info("omnichannel.stream.teardown_error", extra={"org_id": org_id})
        log.info("omnichannel.stream.closed", extra={"org_id": org_id, "user_id": user_id})
