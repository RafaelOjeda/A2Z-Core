"""Generic inbound webhook dispatch (§5.6).

One route for every channel (``POST /webhooks/{channel_type}/{connection_id}``,
mounted in ``app/routers/omnichannel.py``): resolve the connection ->
verify the signature via the adapter registry -> ack fast by enqueueing to
the shared inbound SQS queue. Adding a channel touches no code here -- only
``adapters/`` + the registry (§5.2 extensibility invariant).
"""

from __future__ import annotations

import json
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secrets
from app.core.logging import get_logger
from app.services.omnichannel import metrics, queues
from app.services.omnichannel.adapters.registry import get_adapter
from app.services.omnichannel.exceptions import ConnectionNotFoundError, WebhookSignatureError
from app.services.omnichannel.models import ChannelConnection

log = get_logger("omnichannel.webhooks")


async def _load_connection(session: AsyncSession, connection_id: str) -> ChannelConnection:
    result = await session.execute(
        select(ChannelConnection).where(ChannelConnection.id == connection_id)
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise ConnectionNotFoundError(f"No channel connection {connection_id!r}")
    if connection.status != "active":
        # A disabled connection (connections.py::disable_connection) is
        # treated exactly like an unknown one -- its webhook URL must stop
        # working the moment it's disabled, not just stop appearing in the
        # UI (API review, 2026-07-18).
        raise ConnectionNotFoundError(f"Channel connection {connection_id!r} is not active")
    return connection


async def _resolve_connection(
    session: AsyncSession, channel_type: str, connection_id: str
) -> ChannelConnection:
    connection = await _load_connection(session, connection_id)
    if connection.channel_type != channel_type:
        raise ConnectionNotFoundError(
            f"Connection {connection_id!r} is not a {channel_type!r} connection"
        )
    return connection


async def handle_webhook(
    session: AsyncSession,
    channel_type: str,
    connection_id: str,
    raw_body: bytes,
    headers: dict[str, str],
) -> None:
    """Verify and enqueue one inbound webhook call.

    Raises:
        ConnectionNotFoundError: ``connection_id`` doesn't resolve to a
            connection, or resolves to a different ``channel_type`` (treated
            the same way -- it's simply not this connection's URL).
        WebhookSignatureError: The signature didn't verify.

    Performance target: < 2s p99 -- ack fast, do the real work in the worker
    (§5.6; Meta's retry window is ~10s). Emits ``WebhookAckLatencyMs``, the
    series that target is alarmed on (§11).
    """
    started = time.perf_counter()
    connection = await _resolve_connection(session, channel_type, connection_id)

    adapter = get_adapter(channel_type)
    secret_bundle = await secrets.get_secret(
        connection.org_id, "omnichannel", connection.credentials_secret_key
    )
    signing_secret = secret_bundle.get("app_secret", "")
    if not await adapter.verify_inbound_signature(raw_body, headers, signing_secret):
        raise WebhookSignatureError(
            f"Signature verification failed for connection {connection_id!r}"
        )

    raw_payload = json.loads(raw_body.decode("utf-8"))
    await queues.enqueue_inbound(
        org_id=connection.org_id,
        channel_type=channel_type,
        connection_id=connection.id,
        raw_payload=raw_payload,
    )
    # Measured over the accepted path only. A rejected webhook (bad signature /
    # unknown connection) is an auth outcome, not an ack -- folding those into
    # the latency series would let a burst of cheap 401s mask a real p99 breach.
    metrics.record_webhook_ack_latency(channel_type, (time.perf_counter() - started) * 1000)
    log.info(
        "omnichannel.webhook.accepted",
        extra={
            "org_id": connection.org_id,
            "channel_type": channel_type,
            "connection_id": connection_id,
        },
    )


async def verify_subscription(
    session: AsyncSession, channel_type: str, connection_id: str, params: dict[str, str]
) -> str:
    """Answer a provider's webhook-subscription verification handshake (§5.6).

    The ``GET`` counterpart to ``handle_webhook``'s ``POST``: some providers
    (Meta's Cloud API) require this handshake to succeed once before they'll
    ever deliver a real webhook to the URL, so without it a WhatsApp
    connection could never actually go live (API review, 2026-07-18).

    Raises:
        ConnectionNotFoundError: ``connection_id`` doesn't resolve to a
            connection, or resolves to a different ``channel_type``.
        ChannelAdapterError: This channel has no such handshake, or the
            request doesn't check out.
    """
    connection = await _resolve_connection(session, channel_type, connection_id)
    adapter = get_adapter(channel_type)
    secret_bundle = await secrets.get_secret(
        connection.org_id, "omnichannel", connection.credentials_secret_key
    )
    return await adapter.verify_subscription(params, secret_bundle)
