"""CloudWatch custom metrics for the ``A2Z/OmniChannel`` namespace (§11).

Observability at MVP is CloudWatch-only (⚠ ADAPTED: no Sentry, §7). Structured
JSON logs via ``core.logging`` remain the primary trace; this module is
strictly the *numeric* side -- the series the §11 alarms watch:

  * ``WebhookAckLatencyMs``       per channel (alarm p99 > 2s -- Meta's retry
                                  window is ~10s, §5.6)
  * ``MessageProcessingLatencyMs`` receipt -> visible in inbox
  * ``RoutingLatencyMs``          assignment decision time
  * ``SendSuccessRate`` / ``SendFailureRate`` per channel (alarm failure > 5%)
  * ``ActiveSSEStreams``          becomes ActiveAppSyncConnections at distribution

**Metrics never break the flow they measure.** Every publish is best-effort:
failures are logged and swallowed, never raised. A CloudWatch outage (or
throttle) must not fail a customer's inbound message or an agent's reply --
that trade is deliberate and is why these calls don't use the typed-error
convention the rest of the service follows.

Emission is fire-and-forget via ``asyncio`` background tasks so a PutMetricData
round-trip never sits in the request/worker hot path. Rates are emitted as
count-based success/failure series (1.0 per event) -- CloudWatch computes the
actual rate at alarm time, which keeps the emit side stateless.
"""

from __future__ import annotations

import asyncio

from app.core import clients
from app.core.logging import get_logger

log = get_logger("omnichannel.metrics")

NAMESPACE = "A2Z/OmniChannel"

# Background emit tasks, held so they aren't garbage-collected mid-flight
# (asyncio only keeps weak references to tasks) and so tests can await them.
_pending: set[asyncio.Task[None]] = set()


async def _put(name: str, value: float, unit: str, dimensions: dict[str, str]) -> None:
    """Publish one datum. Best-effort: never raises into the caller's flow."""
    try:
        await clients.run_aws(
            clients.cloudwatch().put_metric_data,
            Namespace=NAMESPACE,
            MetricData=[
                {
                    "MetricName": name,
                    "Value": value,
                    "Unit": unit,
                    "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 -- metrics must never break the flow
        log.info("omnichannel.metric.failed", extra={"metric": name, "error": str(exc)})


def _emit(name: str, value: float, unit: str, dimensions: dict[str, str]) -> None:
    """Schedule a metric publish without awaiting the CloudWatch round-trip."""
    try:
        task = asyncio.create_task(_put(name, value, unit, dimensions))
    except RuntimeError:
        # No running loop (e.g. called from sync context) -- drop rather than
        # raise; metrics are never worth breaking a caller over.
        log.info("omnichannel.metric.no_loop", extra={"metric": name})
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Await any in-flight metric publishes. For tests and worker shutdown."""
    while _pending:
        await asyncio.gather(*tuple(_pending), return_exceptions=True)


def record_webhook_ack_latency(channel_type: str, elapsed_ms: float) -> None:
    """Webhook receipt -> ack. Alarmed at p99 > 2s (§11)."""
    _emit("WebhookAckLatencyMs", elapsed_ms, "Milliseconds", {"ChannelType": channel_type})


def record_message_processing_latency(channel_type: str, elapsed_ms: float) -> None:
    """Queue receipt -> message visible in the inbox (§11)."""
    _emit("MessageProcessingLatencyMs", elapsed_ms, "Milliseconds", {"ChannelType": channel_type})


def record_routing_latency(elapsed_ms: float) -> None:
    """Time spent deciding/recording an assignment (§11)."""
    _emit("RoutingLatencyMs", elapsed_ms, "Milliseconds", {})


def record_send_result(channel_type: str, *, success: bool) -> None:
    """One outbound send outcome. Alarmed at failure rate > 5% (§11)."""
    name = "SendSuccessRate" if success else "SendFailureRate"
    _emit(name, 1.0, "Count", {"ChannelType": channel_type})


def record_stream_delta(delta: int) -> None:
    """+1 on SSE connect, -1 on disconnect (``ActiveSSEStreams``, §11).

    A delta rather than a gauge: each API process only knows its own streams,
    so the absolute count is CloudWatch's SUM across the fleet -- which stays
    correct when this becomes ActiveAppSyncConnections at distribution.
    """
    _emit("ActiveSSEStreams", float(delta), "Count", {})
