"""The ``ChannelAdapter`` contract (CLAUDE.md §7, §5.2).

Every channel implements this Protocol. One file per channel; nothing else in
the system -- worker, routing, the unified inbox -- may know which channel
it's talking to beyond this contract. Adding a channel (Instagram, SMS) is one
new adapter file plus one registry entry (registry.py); this file never
changes for a new channel.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.services.omnichannel.adapters.types import (
    DeliveryStatusUpdate,
    NormalizedInboundMessage,
    OutboundContent,
    SendResult,
    SupportedFeatures,
)


@runtime_checkable
class ChannelAdapter(Protocol):
    """Every channel implements this. One file per channel. Nothing else in
    the system may know which channel it's talking to beyond this contract."""

    supported_features: SupportedFeatures

    async def verify_inbound_signature(
        self, raw_body: bytes, headers: dict[str, str], secret: str
    ) -> bool:
        """Verify an inbound webhook's signature before anything else runs (§5.6)."""
        ...

    async def verify_subscription(self, params: dict[str, str], credentials: dict[str, Any]) -> str:
        """Answer a provider's webhook-subscription verification handshake
        (e.g. Meta's ``GET .../webhooks/...?hub.mode=subscribe&hub.challenge=...``,
        API review 2026-07-18).

        Args:
            params: The request's query params, provider-shaped (e.g. Meta's
                ``hub.mode``/``hub.verify_token``/``hub.challenge``).
            credentials: This connection's secret bundle (same shape
                ``send_outbound`` receives), expected to carry whatever the
                channel needs to check the request is genuine.

        Returns:
            The challenge value to echo back verbatim.

        Raises:
            ChannelAdapterError: This channel has no such handshake, or the
                request doesn't check out (wrong/missing verify token).
        """
        ...

    async def normalize_inbound(
        self, raw_payload: dict[str, Any]
    ) -> list[NormalizedInboundMessage]:
        """Turn a channel-specific inbound payload into channel-agnostic messages."""
        ...

    async def send_outbound(
        self, to: str, content: OutboundContent, credentials: dict[str, Any]
    ) -> SendResult:
        """Send one outbound message through this channel."""
        ...

    async def interpret_delivery_webhook(
        self, raw_payload: dict[str, Any]
    ) -> list[DeliveryStatusUpdate]:
        """Turn a channel-specific delivery-status payload into status updates."""
        ...
