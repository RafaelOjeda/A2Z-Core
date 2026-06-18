"""Load / latency tests for Core hot paths (Design §5.3, §5.4).

Run explicitly: ``pytest tests/load -m load -v``. These run against moto (not real
AWS), so the absolute numbers are a smoke check of the design targets, not a
production SLA measurement — the true targets are validated against LocalStack/AWS.
We measure per-call latency (p99) sequentially to avoid thread-pool contention
skewing single-call numbers, plus a concurrency batch for throughput.

Targets (Design §5.4):
  * get_membership  p99 < 50ms
  * log_audit       p99 < 50ms
  * send_email          < 500ms
"""

from __future__ import annotations

import asyncio
import statistics
import time

import pytest

from app.core import audit, email, membership
from app.core.audit import ActionType
from app.core.email import ServiceType
from app.core.membership import Role

pytestmark = [pytest.mark.load, pytest.mark.integration]


def _p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    idx = max(0, int(len(ordered) * 0.99) - 1)
    return ordered[idx]


async def test_get_membership_latency(aws: None) -> None:
    org = await membership.create_org("Load Org", "owner")
    users = [f"user-{i}" for i in range(50)]
    for u in users:
        await membership.add_member(org.org_id, u, Role.MEMBER, "owner")

    latencies: list[float] = []
    for i in range(300):
        u = users[i % len(users)]
        start = time.perf_counter()
        m = await membership.get_membership(u, org.org_id)
        latencies.append(time.perf_counter() - start)
        assert m is not None

    p99 = _p99(latencies) * 1000
    avg = statistics.mean(latencies) * 1000
    print(f"\nget_membership: avg={avg:.2f}ms p99={p99:.2f}ms (n={len(latencies)})")
    assert p99 < 50, f"p99 {p99:.2f}ms exceeds 50ms target"


async def test_log_audit_latency(aws: None) -> None:
    latencies: list[float] = []
    for i in range(300):
        start = time.perf_counter()
        await audit.log_audit("load-org", "actor", ActionType.MEMBER_ADDED, "user", f"u{i}")
        latencies.append(time.perf_counter() - start)

    p99 = _p99(latencies) * 1000
    avg = statistics.mean(latencies) * 1000
    print(f"\nlog_audit: avg={avg:.2f}ms p99={p99:.2f}ms (n={len(latencies)})")
    assert p99 < 50, f"p99 {p99:.2f}ms exceeds 50ms target"


async def test_send_email_latency(aws: None) -> None:
    latencies: list[float] = []
    for i in range(40):
        start = time.perf_counter()
        await email.send_email(
            "load-email-org", ServiceType.INVOICING, f"c{i}@example.com",
            "Subject", "<p>body</p>",
        )
        latencies.append(time.perf_counter() - start)

    p99 = _p99(latencies) * 1000
    avg = statistics.mean(latencies) * 1000
    print(f"\nsend_email: avg={avg:.2f}ms p99={p99:.2f}ms (n={len(latencies)})")
    assert p99 < 500, f"p99 {p99:.2f}ms exceeds 500ms target"


async def test_membership_concurrent_throughput(aws: None) -> None:
    """Design §5.3: many concurrent get_membership calls complete quickly."""
    org = await membership.create_org("Throughput Org", "owner")
    users = [f"u-{i}" for i in range(20)]
    for u in users:
        await membership.add_member(org.org_id, u, Role.MEMBER, "owner")

    start = time.perf_counter()
    results = await asyncio.gather(
        *(membership.get_membership(users[i % len(users)], org.org_id) for i in range(500))
    )
    elapsed = time.perf_counter() - start
    print(f"\n500 concurrent get_membership in {elapsed:.2f}s")
    assert all(r is not None for r in results)
    assert elapsed < 10
