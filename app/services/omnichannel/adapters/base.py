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
