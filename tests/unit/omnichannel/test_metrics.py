"""Unit tests for the A2Z/OmniChannel CloudWatch metrics module (§11, Step 8).

The load-bearing property here is the one the module promises: **metrics never
break the flow they measure**. A CloudWatch outage/throttle must not fail a
customer's inbound message or an agent's reply, so every failure mode below
asserts "swallowed, not raised".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from app.core import clients
from app.services.omnichannel import metrics


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Swap in a cloudwatch client that records PutMetricData kwargs."""
    calls: list[dict[str, Any]] = []

    def _put_metric_data(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {}

    fake = Mock()
    fake.put_metric_data = _put_metric_data
    monkeypatch.setattr(clients, "cloudwatch", lambda: fake)
    return calls


async def test_webhook_ack_latency_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture(monkeypatch)

    metrics.record_webhook_ack_latency("whatsapp", 123.4)
    await metrics.drain()

    assert len(calls) == 1
    assert calls[0]["Namespace"] == "A2Z/OmniChannel"
    datum = calls[0]["MetricData"][0]
    assert datum["MetricName"] == "WebhookAckLatencyMs"
    assert datum["Value"] == 123.4
    assert datum["Unit"] == "Milliseconds"
    assert datum["Dimensions"] == [{"Name": "ChannelType", "Value": "whatsapp"}]


async def test_message_processing_latency_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture(monkeypatch)

    metrics.record_message_processing_latency("email", 45.0)
    await metrics.drain()

    datum = calls[0]["MetricData"][0]
    assert datum["MetricName"] == "MessageProcessingLatencyMs"
    assert datum["Dimensions"] == [{"Name": "ChannelType", "Value": "email"}]


async def test_routing_latency_has_no_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture(monkeypatch)

    metrics.record_routing_latency(7.5)
    await metrics.drain()

    datum = calls[0]["MetricData"][0]
    assert datum["MetricName"] == "RoutingLatencyMs"
    assert datum["Dimensions"] == []


@pytest.mark.parametrize(
    ("success", "expected"),
    [(True, "SendSuccessRate"), (False, "SendFailureRate")],
)
async def test_send_result_series(
    monkeypatch: pytest.MonkeyPatch, success: bool, expected: str
) -> None:
    calls = _capture(monkeypatch)

    metrics.record_send_result("whatsapp", success=success)
    await metrics.drain()

    datum = calls[0]["MetricData"][0]
    assert datum["MetricName"] == expected
    # Count-based: 1.0 per event, CloudWatch computes the rate at alarm time.
    assert datum["Value"] == 1.0
    assert datum["Unit"] == "Count"


async def test_stream_delta_signs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture(monkeypatch)

    metrics.record_stream_delta(1)
    metrics.record_stream_delta(-1)
    await metrics.drain()

    values = [c["MetricData"][0]["Value"] for c in calls]
    assert values == [1.0, -1.0]
    assert all(c["MetricData"][0]["MetricName"] == "ActiveSSEStreams" for c in calls)


async def test_cloudwatch_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CloudWatch error must never surface to the measured flow."""
    fake = Mock()
    fake.put_metric_data = Mock(
        side_effect=ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "PutMetricData"
        )
    )
    monkeypatch.setattr(clients, "cloudwatch", lambda: fake)

    metrics.record_webhook_ack_latency("whatsapp", 10.0)
    await metrics.drain()  # must not raise

    fake.put_metric_data.assert_called_once()


async def test_client_construction_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a broken client factory must not break the caller."""

    def _boom() -> Any:
        raise RuntimeError("no credentials")

    monkeypatch.setattr(clients, "cloudwatch", _boom)

    metrics.record_routing_latency(1.0)
    await metrics.drain()  # must not raise


async def test_emit_is_non_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    """record_* returns before the PutMetricData round-trip completes."""
    calls = _capture(monkeypatch)

    metrics.record_routing_latency(1.0)
    # Nothing published yet -- the emit was scheduled, not awaited.
    assert calls == []

    await metrics.drain()
    assert len(calls) == 1
