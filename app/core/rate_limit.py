"""Rate limiting — Redis sliding-window limiter (CLAUDE.md §7).

A sorted set per ``{org_id, action}`` holds one member per recent request, scored
by timestamp (ms). On each call we, in a single pipeline: drop entries older than
the window, add the current request, count, and refresh the key's TTL. If the
count exceeds the limit we remove our own entry and raise
:class:`RateLimitError` with ``retry_after``.

This "add then check" keeps the window read+write atomic enough to avoid races
without a Lua script (so it runs on fakeredis in tests). Default limits live in
``config.RATE_LIMITS`` so services don't invent their own (CLAUDE.md §7).
Key: ``ratelimit:{org_id}:{action}``. Performance: < 10ms.
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Any

from app.config import RATE_LIMITS
from app.core import clients
from app.core.exceptions import RateLimitError


def _key(org_id: str, action: str) -> str:
    return f"ratelimit:{org_id}:{action}"


def limits_for(action: str) -> tuple[int, int]:
    """Return ``(limit, window_seconds)`` for an action from the config registry.

    Raises:
        KeyError: The action has no configured default.
    """
    return RATE_LIMITS[action]


async def check_and_increment(
    org_id: str,
    action: str,
    *,
    limit: int,
    window_seconds: int,
) -> None:
    """Record a request and enforce a sliding-window limit.

    Raises:
        RateLimitError: The limit is exceeded; carries ``retry_after`` seconds.

    Performance: < 10ms.
    """
    redis = clients.redis_client()
    key = _key(org_id, action)
    now = time.time()
    now_ms = int(now * 1000)
    window_start_ms = now_ms - window_seconds * 1000
    member = f"{now_ms}:{uuid.uuid4().hex}"

    async with redis.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, 0, window_start_ms)
        pipe.zadd(key, {member: now_ms})
        pipe.zcard(key)
        pipe.expire(key, window_seconds)
        results = await pipe.execute()

    count = int(results[2])
    if count > limit:
        # We optimistically added ourselves; roll back and reject.
        await redis.zrem(key, member)
        retry_after = await _retry_after(redis, key, now, window_seconds)
        raise RateLimitError(
            f"Rate limit exceeded for {action} (limit {limit}/{window_seconds}s)",
            retry_after=retry_after,
        )


async def _retry_after(redis: Any, key: str, now: float, window_seconds: int) -> int:
    """Seconds until the oldest in-window request ages out (>= 1)."""
    oldest = await redis.zrange(key, 0, 0, withscores=True)
    if not oldest:
        return window_seconds
    oldest_ms = float(oldest[0][1])
    seconds_left = (oldest_ms / 1000 + window_seconds) - now
    return max(1, math.ceil(seconds_left))
