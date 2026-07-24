"""Load / latency tests for Omni-Channel hot paths (§11, Build Order Step 8).

Run explicitly: ``pytest tests/load -m load -v``. These run against moto +
fakeredis + a local Postgres, so the absolute numbers are a smoke check of the
§11 targets, not a production SLA measurement -- same caveat as Core's own load
suite. What they do catch is an order-of-magnitude regression (an N+1, a
synchronous AWS call added to a hot path).

Targets:
  * webhook ack        p99 < 2s   (§5.6/§11 -- Meta's retry window is ~10s, and
                                   this is the series the §11 alarm watches)
  * inbound processing p99 < 2s   (§3 "within a couple of seconds":
                                   receipt -> visible in the inbox)
  * realtime relay         < 100ms (§6.2 publish_update target, measured
                                   publish -> frame out of the SSE relay)

CloudWatch is stubbed to a no-op: metric emission is a fire-and-forget
background task by design (metrics.py), so leaving the real client in would
measure moto's credential failures rather than the code under test.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import statistics
import time
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clients, realtime
from app.services.omnichannel import metrics, queues, stream, webhooks, worker
from app.services.omnichannel.models import ChannelConnection

pytestmark = [pytest.mark.load, pytest.mark.integration]

_APP_SECRET = "wa-app-secret"


def _p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    idx = max(0, int(len(ordered) * 0.99) - 1)
    return ordered[idx]


@pytest.fixture(autouse=True)
def _stub_cloudwatch(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Mock()
    fake.put_metric_data = Mock(return_value={})
    monkeypatch.setattr(clients, "cloudwatch", lambda: fake)


def _sign(raw_body: bytes) -> str:
    mac = hmac.new(_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


async def _seed_secret(org_id: str, key: str) -> None:
    await clients.run_aws(
        clients.secretsmanager().create_secret,
        Name=f"a2z/{org_id}/omnichannel/{key}",
        SecretString=json.dumps(
            {
                "app_secret": _APP_SECRET,
                "access_token": "tok",
                "phone_number_id": "123",
            }
        ),
    )


async def _seed_connection(session: AsyncSession, org_id: str = "org-load") -> ChannelConnection:
    connection = ChannelConnection(
        org_id=org_id,
        channel_type="whatsapp",
        display_name="Load WhatsApp",
        provider_account_id="15550001111",
        credentials_secret_key="whatsapp-main",
        status="active",
    )
    session.add(connection)
    await session.commit()
    await _seed_secret(org_id, connection.credentials_secret_key)
    return connection


def _payload(wamid: str, from_number: str = "15551234567") -> bytes:
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [{"wa_id": from_number, "profile": {"name": "Jane"}}],
                                "messages": [
                                    {
                                        "from": from_number,
                                        "id": wamid,
                                        "type": "text",
                                        "text": {"body": "hello"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    ).encode("utf-8")


async def test_webhook_ack_latency(aws: None, session: AsyncSession) -> None:
    """§5.6/§11: ack fast (< 2s p99) -- validate + enqueue only, no processing."""
    connection = await _seed_connection(session)

    latencies: list[float] = []
    for i in range(50):
        body = _payload(f"wamid.ACK{i}")
        headers = {"X-Hub-Signature-256": _sign(body)}
        start = time.perf_counter()
        await webhooks.handle_webhook(session, "whatsapp", connection.id, body, headers)
        latencies.append(time.perf_counter() - start)

    p99 = _p99(latencies) * 1000
    avg = statistics.mean(latencies) * 1000
    print(f"\nwebhook_ack: avg={avg:.2f}ms p99={p99:.2f}ms (n={len(latencies)})")
    assert p99 < 2000, f"p99 {p99:.2f}ms exceeds the 2s ack target"


async def test_inbound_processing_latency(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3: receipt -> visible in the inbox within a couple of seconds."""
    monkeypatch.setattr(worker, "publish_event", AsyncMock())
    monkeypatch.setattr(worker, "publish_update", AsyncMock())
    connection = await _seed_connection(session)

    for i in range(30):
        body = _payload(f"wamid.PROC{i}", from_number=f"1555000{i:04d}")
        headers = {"X-Hub-Signature-256": _sign(body)}
        await webhooks.handle_webhook(session, "whatsapp", connection.id, body, headers)

    latencies: list[float] = []
    for _ in range(30):
        start = time.perf_counter()
        processed = await worker.process_inbound_batch(session, max_messages=1)
        elapsed = time.perf_counter() - start
        if processed:
            latencies.append(elapsed)

    assert latencies, "expected the worker to drain the enqueued webhooks"
    p99 = _p99(latencies) * 1000
    avg = statistics.mean(latencies) * 1000
    print(f"\ninbound_processing: avg={avg:.2f}ms p99={p99:.2f}ms (n={len(latencies)})")
    assert p99 < 2000, f"p99 {p99:.2f}ms exceeds the 2s processing target"


async def test_realtime_relay_latency() -> None:
    """§6.2: an update reaches a connected stream in < 100ms."""
    gen = stream.stream_events("org-load", "agent-1", heartbeat_seconds=0.01)
    assert await asyncio.wait_for(gen.__anext__(), timeout=2.0) == ": connected\n\n"

    latencies: list[float] = []
    for i in range(30):
        start = time.perf_counter()
        await realtime.publish_update("org-load", "org:org-load:conversations", {"seq": i})
        frame = await asyncio.wait_for(_next_data(gen), timeout=2.0)
        latencies.append(time.perf_counter() - start)
        assert frame.startswith("data: ")

    await gen.aclose()
    p99 = _p99(latencies) * 1000
    avg = statistics.mean(latencies) * 1000
    print(f"\nrealtime_relay: avg={avg:.2f}ms p99={p99:.2f}ms (n={len(latencies)})")
    assert p99 < 100, f"p99 {p99:.2f}ms exceeds the 100ms realtime target"


async def _next_data(gen: object) -> str:
    async for frame in gen:  # type: ignore[attr-defined]
        if frame.startswith("data:"):
            return str(frame)
    raise AssertionError("stream ended before a data frame")


async def test_webhook_ack_concurrent_throughput(aws: None, session: AsyncSession) -> None:
    """Many webhooks landing at once still ack quickly (§5.6 -- providers burst)."""
    connection = await _seed_connection(session)

    async def _ack(i: int) -> None:
        body = _payload(f"wamid.CONC{i}")
        headers = {"X-Hub-Signature-256": _sign(body)}
        await webhooks.handle_webhook(session, "whatsapp", connection.id, body, headers)

    start = time.perf_counter()
    # Sequential rather than gather(): one AsyncSession is not safe to use
    # concurrently, and the ack path shares this request's session. This still
    # catches a per-call regression, which is what the target is about.
    for i in range(100):
        await _ack(i)
    elapsed = time.perf_counter() - start

    print(f"\n100 webhook acks in {elapsed:.2f}s ({elapsed / 100 * 1000:.1f}ms each)")
    assert elapsed < 20


async def test_metrics_emit_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """metrics.record_* must return immediately (fire-and-forget, §11)."""
    slow = Mock()

    def _slow_put(**kwargs: object) -> dict[str, object]:
        time.sleep(0.05)  # 50ms per call, on the boto3 thread
        return {}

    slow.put_metric_data = _slow_put
    monkeypatch.setattr(clients, "cloudwatch", lambda: slow)

    start = time.perf_counter()
    for _ in range(20):
        metrics.record_routing_latency(1.0)
    emit_elapsed = time.perf_counter() - start

    # 20 x 50ms = 1s of CloudWatch work; scheduling it must cost ~nothing.
    print(f"\n20 metric emits scheduled in {emit_elapsed * 1000:.2f}ms")
    assert emit_elapsed < 0.05, "record_* is blocking on the CloudWatch round-trip"
    await metrics.drain()


async def test_queue_enqueue_latency(aws: None) -> None:
    """The webhook ack's own hot path is dominated by this SQS send."""
    latencies: list[float] = []
    for i in range(50):
        start = time.perf_counter()
        await queues.enqueue_inbound(
            org_id="org-load",
            channel_type="whatsapp",
            connection_id="conn-1",
            raw_payload={"seq": i},
        )
        latencies.append(time.perf_counter() - start)

    p99 = _p99(latencies) * 1000
    avg = statistics.mean(latencies) * 1000
    print(f"\nenqueue_inbound: avg={avg:.2f}ms p99={p99:.2f}ms (n={len(latencies)})")
    assert p99 < 500
