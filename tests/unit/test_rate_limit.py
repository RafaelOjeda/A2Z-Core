"""Unit tests for core.rate_limit (fakeredis)."""

from __future__ import annotations

import asyncio

import pytest

from app.core import rate_limit
from app.core.exceptions import RateLimitError


async def test_allows_up_to_limit() -> None:
    for _ in range(5):
        await rate_limit.check_and_increment("org", "act", limit=5, window_seconds=60)


async def test_blocks_over_limit_with_retry_after() -> None:
    for _ in range(3):
        await rate_limit.check_and_increment("org", "act", limit=3, window_seconds=60)
    with pytest.raises(RateLimitError) as exc:
        await rate_limit.check_and_increment("org", "act", limit=3, window_seconds=60)
    assert exc.value.retry_after >= 1
    assert exc.value.status_code == 429


async def test_window_slides() -> None:
    await rate_limit.check_and_increment("org", "slide", limit=1, window_seconds=1)
    with pytest.raises(RateLimitError):
        await rate_limit.check_and_increment("org", "slide", limit=1, window_seconds=1)
    await asyncio.sleep(1.1)
    # Window has passed — call succeeds again.
    await rate_limit.check_and_increment("org", "slide", limit=1, window_seconds=1)


async def test_limits_are_isolated_per_org_and_action() -> None:
    await rate_limit.check_and_increment("org-a", "x", limit=1, window_seconds=60)
    # Different org: own budget.
    await rate_limit.check_and_increment("org-b", "x", limit=1, window_seconds=60)
    # Same org, different action: own budget.
    await rate_limit.check_and_increment("org-a", "y", limit=1, window_seconds=60)


async def test_rejected_call_does_not_consume_budget() -> None:
    await rate_limit.check_and_increment("org", "z", limit=2, window_seconds=60)
    await rate_limit.check_and_increment("org", "z", limit=2, window_seconds=60)
    for _ in range(3):
        with pytest.raises(RateLimitError):
            await rate_limit.check_and_increment("org", "z", limit=2, window_seconds=60)
    # Still exactly 2 recorded — rejected calls rolled themselves back.


def test_limits_for_registry() -> None:
    limit, window = rate_limit.limits_for("email.send")
    assert (limit, window) == (50, 3600)
