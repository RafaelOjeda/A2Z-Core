"""Real-time inbox relay -- the browser-facing subscribe side of §5.4.

`core.realtime.publish_update` (called from the worker and routing) pushes
onto Core's in-process broker; this module is the *other half* at MVP: the
service-owned API process subscribes (via `core.realtime.subscribe`) and
relays to browsers as Server-Sent Events. Per the plan, "Core's job stops at
the publish" -- the SSE relay is deliberately **not** in Core, because at
the distribution phase it disappears entirely (browsers connect straight to
AppSync GraphQL subscriptions; there is no relay to keep). So this is
MVP-only glue, sized accordingly: no new dependency, just a plain async
generator behind a FastAPI streaming response.

The transport is entirely Core's concern -- this module only ever deals in
logical channel names (``org:{org_id}:conversations``,
``user:{user_id}:notifications``); the ``rt:`` prefix and everything below
`core.realtime.subscribe` is a private Core implementation detail.
``test_stream.py`` still locks publish and subscribe together with a
round-trip test so any change to Core's contract fails loudly here.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable

from app.core import realtime
from app.core.logging import get_logger
from app.services.omnichannel import metrics

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
        f"org:{org_id}:conversations",
        f"user:{user_id}:notifications",
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

    Performance: relay latency is one in-process queue hop, < 100ms end to end.
    """
    channels = _channels_for(org_id, user_id)
    started = clock()
    metrics.record_stream_delta(1)
    log.info("omnichannel.stream.opened", extra={"org_id": org_id, "user_id": user_id})
    try:
        async with realtime.subscribe(*channels) as sub:
            yield _frame_comment("connected")
            while True:
                if max_lifetime_seconds is not None and clock() - started >= max_lifetime_seconds:
                    log.info("omnichannel.stream.lifetime", extra={"org_id": org_id})
                    return
                if is_disconnected is not None and await is_disconnected():
                    return
                message = await sub.get(timeout_seconds=heartbeat_seconds)
                if message is None:
                    yield _frame_comment("keepalive")
                    continue
                yield _frame_data(message)
    finally:
        # Paired with the +1 above so the metric tracks live streams; in the
        # finally block so a cancelled/errored stream still decrements
        # (client disconnect surfaces here as CancelledError/GeneratorExit).
        metrics.record_stream_delta(-1)
        log.info("omnichannel.stream.closed", extra={"org_id": org_id, "user_id": user_id})
