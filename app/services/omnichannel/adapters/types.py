"""Shared Pydantic types for channel adapters (CLAUDE.md §7).

One contract, many channels: every adapter speaks these shapes so the rest of
the system (worker, routing, inbox) never branches on which channel it's
touching (§5.2). Fields unused by a given channel are simply left ``None`` /
empty rather than growing channel-specific subclasses.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SupportedFeatures(BaseModel):
    """What a channel can do -- callers branch on capability, not channel identity."""

    templates: bool = False
    rich_media: bool = False
    typing_indicators: bool = False
    read_receipts: bool = False


class OutboundAttachment(BaseModel):
    filename: str
    content_type: str
    content: bytes


class OutboundContent(BaseModel):
    """What an agent is sending. ``subject`` is email-only; others are optional per channel."""

    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    attachments: list[OutboundAttachment] = Field(default_factory=list)


class SendResult(BaseModel):
    external_message_id: str
    status: str


class InboundAttachment(BaseModel):
    filename: str
    content_type: str
    content: bytes


class NormalizedInboundMessage(BaseModel):
    """One inbound message, channel-agnostic -- the shape ``normalize_inbound`` produces."""

    external_id: str
    external_message_id: str
    display_name: str | None = None
    body_text: str | None = None
    content_type: str = "text/plain"
    attachments: list[InboundAttachment] = Field(default_factory=list)


class DeliveryStatusUpdate(BaseModel):
    external_message_id: str
    status: str
