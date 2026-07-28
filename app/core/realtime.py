"""Realtime — fan-out to connected clients (root CLAUDE.md §6.2 gap).

Transport (single-EC2, one-process MVP — see docs/architecture/single-box-mvp.md):
an in-process publish/subscribe broker. The whole platform (API + the
Omni-Channel worker, §5.4/§12) runs as one ``asyncio`` event loop in one
process, so "publish" and "subscribe" are just two ends of the same
in-memory queue -- no network hop, no external broker to run or pay for.
The service-owned API process still does the SSE relay to browsers (Core's
job stops at the publish); only the transport underneath changed. If this
ever needs to cross a process or host boundary again, swap the transport
behind ``publish_update``/``subscribe`` -- callers never change. (The prior
Redis pub/sub incarnation had the same contract; see git history.)

Fire-and-forget from the caller's perspective: publish failures are raised,
not swallowed, but a slow or absent subscriber never blocks the publisher --
publishing never awaits a subscriber's queue.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core.exceptions import RealtimeError
from app.core.logging import get_logger

log = get_logger("core.realtime")

# Bounded per-subscriber inbox. A subscriber that falls behind (a stalled
# browser tab, a slow relay loop) has its oldest undelivered messages
# dropped rather than ever blocking a publisher -- see publish_update.
_QUEUE_MAXSIZE = 100

# channel key ("rt:{channel}") -> the set of live subscriber queues.
_subscribers: dict[str, set[asyncio.Queue[str]]] = {}


def _channel_key(channel: str) -> str:
    return f"rt:{channel}"


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
        RealtimeError: The payload could not be serialized.

    Performance: < 100ms.
    """
    try:
        message = json.dumps({**payload, "org_id": org_id}, default=str)
    except TypeError as exc:
        log.error("realtime.publish_failed", extra={"org_id": org_id, "channel": channel})
        raise RealtimeError(f"Failed to publish update on {channel}: {exc}") from exc

    key = _channel_key(channel)
    # Snapshot the subscriber set before iterating -- subscribe()/unsubscribe
    # can mutate it, and we never want to hold it open across an await
    # (there isn't one here, but keep the invariant explicit for future edits).
    for queue in list(_subscribers.get(key, ())):
        _put_dropping_oldest(queue, message)

    log.info("realtime.published", extra={"org_id": org_id, "channel": channel})


def _put_dropping_oldest(queue: asyncio.Queue[str], message: str) -> None:
    """Enqueue ``message``, discarding the oldest entry if the queue is full.

    Never awaits: a publisher must not block on a slow subscriber. SSE is a
    latest-state feed and the browser reconnects on focus/lifetime-cap, so
    dropping the oldest undelivered frame is the right lossy behavior.
    """
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - can't race on one loop
            pass
        queue.put_nowait(message)


class Subscription:
    """A live subscription handle returned by :func:`subscribe`."""

    def __init__(self, queue: asyncio.Queue[str]) -> None:
        self._queue = queue

    async def get(self, timeout_seconds: float | None = None) -> str | None:
        """Return the next published message, or ``None`` after ``timeout_seconds``.

        Mirrors ``redis.asyncio.client.PubSub.get_message(timeout=...)``'s
        contract: a timeout is not an error, it's "nothing arrived yet" --
        callers (the SSE relay) use ``None`` to emit a heartbeat. (Parameter
        named ``timeout_seconds`` rather than ``timeout`` -- ruff's ASYNC109
        flags an ``async def`` parameter literally named ``timeout`` as a
        footgun, nudging toward ``asyncio.timeout()`` at the call site
        instead; we still want a per-call timeout here, so we rename instead
        of suppressing the rule.)
        """
        if timeout_seconds is None:
            return await self._queue.get()
        try:
            async with asyncio.timeout(timeout_seconds):
                return await self._queue.get()
        except TimeoutError:
            return None


@asynccontextmanager
async def subscribe(*channels: str) -> AsyncIterator[Subscription]:
    """Subscribe to one or more logical channels for the duration of the block.

    Args:
        channels: Logical channel names (the ``rt:`` transport prefix is
            applied internally -- callers never see it).

    Yields:
        A :class:`Subscription` whose ``get()`` returns each published
        message in order, or ``None`` on idle timeout.

    Performance: subscribe/unsubscribe is O(len(channels)); relay latency is
    one in-process queue hop, < 100ms end to end.
    """
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    keys = [_channel_key(c) for c in channels]
    for key in keys:
        _subscribers.setdefault(key, set()).add(queue)
    try:
        yield Subscription(queue)
    finally:
        for key in keys:
            subs = _subscribers.get(key)
            if subs is None:
                continue
            subs.discard(queue)
            if not subs:
                del _subscribers[key]


def reset() -> None:
    """Clear all subscriber state. Used by tests between runs."""
    _subscribers.clear()
