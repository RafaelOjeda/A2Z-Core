"""Rate limiting — in-process sliding-window limiter (CLAUDE.md §7).

A deque per ``{org_id, action}`` holds one timestamp (monotonic seconds) per
recent request. On each call we evict entries older than the window, then
check whether the deque is already at the limit before recording the new
request. Default limits live in ``config.RATE_LIMITS`` so services don't
invent their own (CLAUDE.md §7). Key: ``(org_id, action)``.
Performance: < 10ms (in practice, microseconds — no I/O).

Single-process only (docs/architecture/single-box-mvp.md): this state is a
plain module-global dict, not shared across processes. Running more than one
worker process would give each its own budget per action — silently
doubling every limit, including provider pair-rate ceilings like
``omnichannel.whatsapp.send``. Do not add a second process without revisiting
this module.

DOCUMENTED DEVIATION from the prior Redis-backed implementation: that version
used an "add optimistically, then roll back on overflow" dance to keep the
window read+write atomic across a network hop (a Redis pipeline). On a
single event loop with no ``await`` in the critical section there is no race
to protect against, so this version simply checks the limit before
recording -- there is nothing to roll back.
"""

from __future__ import annotations

import math
import time
from collections import deque

from app.config import RATE_LIMITS
from app.core.exceptions import RateLimitError

_windows: dict[tuple[str, str], deque[float]] = {}


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
    now = time.monotonic()
    window_start = now - window_seconds
    key = (org_id, action)
    dq = _windows.setdefault(key, deque())

    while dq and dq[0] <= window_start:
        dq.popleft()

    if len(dq) >= limit:
        retry_after = max(1, math.ceil(dq[0] + window_seconds - now))
        raise RateLimitError(
            f"Rate limit exceeded for {action} (limit {limit}/{window_seconds}s)",
            retry_after=retry_after,
        )

    dq.append(now)


def reset() -> None:
    """Clear all rate-limit state. Used by tests between runs."""
    _windows.clear()
