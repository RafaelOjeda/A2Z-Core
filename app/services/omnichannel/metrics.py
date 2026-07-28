"""Metrics for Omni-Channel's hot paths (§11) -- structured logs, not CloudWatch.

Single-box MVP (docs/architecture/single-box-mvp.md): there is no CloudWatch
agent shipping logs off this box and no alarms watching a numeric series, so
a custom-metrics API namespace was pure cost with no consumer. Every
``record_*`` call here now writes one structured JSON log line via
``core.logging`` instead of a `PutMetricData` call -- same call sites, same
function names (nothing above this module changes), same fields (channel
type, latency, delta), just a different sink. If/when this needs to
distribute again and something is actually watching these numbers,
re-introduce a real metrics backend behind these same functions.

**Metrics never break the flow they measure.** A logging failure (a broken
handler, a misconfigured sink) must not fail a customer's inbound message or
an agent's reply, so every ``record_*`` call is wrapped -- overkill for a
plain ``logger.info`` today, but the invariant is worth keeping explicit
rather than silently dropping it because the failure mode got less likely.
"""

from __future__ import annotations

from app.core.logging import get_logger

log = get_logger("omnichannel.metrics")


def _emit(event: str, **fields: object) -> None:
    try:
        log.info(event, extra=fields)
    except Exception as exc:  # noqa: BLE001 -- metrics must never break the flow
        # Deliberately not re-using `log` here -- if logging itself is what
        # failed, calling it again is how you get a crash loop instead of a
        # dropped metric.
        print(f"omnichannel.metric.failed event={event} error={exc}")  # noqa: T201


def record_webhook_ack_latency(channel_type: str, elapsed_ms: float) -> None:
    """Webhook receipt -> ack. Was alarmed at p99 > 2s (§11); now just logged."""
    _emit("omnichannel.metric.webhook_ack_latency_ms", channel_type=channel_type, value=elapsed_ms)


def record_message_processing_latency(channel_type: str, elapsed_ms: float) -> None:
    """Queue receipt -> message visible in the inbox (§11)."""
    _emit(
        "omnichannel.metric.message_processing_latency_ms",
        channel_type=channel_type,
        value=elapsed_ms,
    )


def record_routing_latency(elapsed_ms: float) -> None:
    """Time spent deciding/recording an assignment (§11)."""
    _emit("omnichannel.metric.routing_latency_ms", value=elapsed_ms)


def record_send_result(channel_type: str, *, success: bool) -> None:
    """One outbound send outcome. Was alarmed at failure rate > 5% (§11)."""
    _emit("omnichannel.metric.send_result", channel_type=channel_type, success=success)


def record_stream_delta(delta: int) -> None:
    """+1 on SSE connect, -1 on disconnect (was ``ActiveSSEStreams``, §11)."""
    _emit("omnichannel.metric.stream_delta", value=delta)
