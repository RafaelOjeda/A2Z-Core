"""Unit tests for Omni-Channel's metrics module (§11, single-box MVP).

Structured logs, not CloudWatch (see metrics.py's module docstring). The
load-bearing property is unchanged from the CloudWatch-backed version:
**metrics never break the flow they measure** -- a logging failure must be
swallowed, not raised.
"""

from __future__ import annotations

import logging

import pytest

from app.services.omnichannel import metrics


def test_webhook_ack_latency_shape(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="a2z.omnichannel.metrics"):
        metrics.record_webhook_ack_latency("whatsapp", 123.4)

    record = caplog.records[0]
    assert record.message == "omnichannel.metric.webhook_ack_latency_ms"
    assert record.channel_type == "whatsapp"
    assert record.value == 123.4


def test_message_processing_latency_shape(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="a2z.omnichannel.metrics"):
        metrics.record_message_processing_latency("email", 45.0)

    record = caplog.records[0]
    assert record.message == "omnichannel.metric.message_processing_latency_ms"
    assert record.channel_type == "email"


def test_routing_latency_has_no_channel_dimension(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="a2z.omnichannel.metrics"):
        metrics.record_routing_latency(7.5)

    record = caplog.records[0]
    assert record.message == "omnichannel.metric.routing_latency_ms"
    assert not hasattr(record, "channel_type")
    assert record.value == 7.5


@pytest.mark.parametrize("success", [True, False])
def test_send_result_shape(caplog: pytest.LogCaptureFixture, success: bool) -> None:
    with caplog.at_level(logging.INFO, logger="a2z.omnichannel.metrics"):
        metrics.record_send_result("whatsapp", success=success)

    record = caplog.records[0]
    assert record.message == "omnichannel.metric.send_result"
    assert record.success is success


def test_stream_delta_signs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="a2z.omnichannel.metrics"):
        metrics.record_stream_delta(1)
        metrics.record_stream_delta(-1)

    values = [r.value for r in caplog.records]
    assert values == [1, -1]
    assert all(r.message == "omnichannel.metric.stream_delta" for r in caplog.records)


def test_logging_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken logger must never surface to the measured flow."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(metrics.log, "info", _boom)

    metrics.record_webhook_ack_latency("whatsapp", 10.0)  # must not raise
