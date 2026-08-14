"""Email channel adapter -- wraps ``core.email``, nothing else (CLAUDE.md §5.2, §13 Step 3).

Built first because it validates the ``ChannelAdapter`` pattern with the least
new surface area: outbound reuses ``core.email.send_email`` almost entirely
(suppression, rate limiting, audit, config-set isolation all come for free),
and inbound is stdlib MIME parsing, no new dependency (§7).

Two Protocol methods are near-total no-ops for this channel, and that's
correct, not a shortcut:

- ``verify_inbound_signature`` -- inbound email never arrives through the
  generic webhook route (§5.6). It's a service-owned pipeline (SES receipt
  rule -> S3 -> the shared inbound SQS queue, §5.2), so there is no HTTP
  webhook signature to check. Returns ``True`` unconditionally so the method
  exists to satisfy the Protocol.
- ``interpret_delivery_webhook`` -- Core's own SES/SNS Lambda already turns
  bounces/complaints into ``email.bounced`` / ``email.complained`` events
  (root CLAUDE.md §8). This method exists so a future subscriber can fold
  those Core events into the same ``DeliveryStatusUpdate`` shape every other
  channel produces; it is not invoked by an actual per-channel webhook.
"""

from __future__ import annotations

import base64
from email import message_from_bytes
from email.message import Message as MimeMessage
from typing import Any

from app.core.email import ServiceType, send_email
from app.core.exceptions import CoreError
from app.core.storage import download_file
from app.services.omnichannel.adapters.types import (
    DeliveryStatusUpdate,
    InboundAttachment,
    NormalizedInboundMessage,
    OutboundContent,
    SendResult,
    SupportedFeatures,
)
from app.services.omnichannel.exceptions import ChannelAdapterError

# Core's `email.bounced` / `email.complained` events, plus SES's own
# "delivered" event, folded into the three statuses every channel reports.
_DELIVERY_STATUS_MAP = {
    "delivered": "delivered",
    "bounced": "failed",
    "complained": "failed",
    "failed": "failed",
}


class EmailAdapter:
    """Adapts ``core.email`` to the ``ChannelAdapter`` Protocol (§5.2, §7)."""

    supported_features = SupportedFeatures(rich_media=True, requires_credentials=False)
    # No webhook to sign (see verify_inbound_signature below).
    signing_secret_key = ""

    async def verify_inbound_signature(
        self, raw_body: bytes, headers: dict[str, str], secret: str
    ) -> bool:
        """Always ``True``: inbound email has no HTTP webhook to sign (§5.2)."""
        return True

    async def verify_subscription(self, params: dict[str, str], credentials: dict[str, Any]) -> str:
        """Always raises: email has no webhook-subscription handshake to answer

        (inbound email is a service-owned SES receipt pipeline, §5.2 -- same
        reasoning as ``verify_inbound_signature`` always returning ``True``).
        """
        raise ChannelAdapterError("Email has no webhook subscription handshake")

    async def normalize_inbound(
        self, raw_payload: dict[str, Any]
    ) -> list[NormalizedInboundMessage]:
        """Parse raw MIME (fetched from S3 by the worker, §5.2) into one message.

        ``raw_payload`` is ``{"raw_mime": bytes, "external_message_id": str}``.
        The worker supplies ``external_message_id`` from the SES notification
        (``mail.messageId``) up front, since the idempotency unique
        constraint (``models.py::uq_message_idempotency``) is keyed on it.
        """
        raw_mime: bytes = raw_payload["raw_mime"]
        external_message_id: str = raw_payload["external_message_id"]
        mime = message_from_bytes(raw_mime)

        from_addr = mime.get("From", "")
        body_text, content_type, attachments = _extract_body(mime)

        return [
            NormalizedInboundMessage(
                external_id=from_addr,
                external_message_id=external_message_id,
                display_name=from_addr,
                body_text=body_text,
                content_type=content_type,
                attachments=attachments,
            )
        ]

    async def send_outbound(
        self, to: str, content: OutboundContent, credentials: dict[str, Any]
    ) -> SendResult:
        """Send via ``core.email.send_email`` -- never boto3 SES directly (§5.2).

        Email has no per-org channel secret to fetch via ``core.secrets``, so
        ``credentials`` carries only ``org_id``. By convention, every caller
        includes ``org_id`` in this dict (alongside real secrets for channels
        that need them, e.g. WhatsApp's ``core.secrets.get_secret`` result) so
        every adapter can rely on the same key without widening the Protocol
        signature to add an explicit ``org_id`` parameter.

        Raises:
            ChannelAdapterError: ``credentials['org_id']`` is missing, or
                ``send_email`` raised any ``CoreError`` (suppression,
                over-limit, invalid address, ...). Core's own error keeps its
                message but is re-raised as ``ChannelAdapterError`` so it
                follows the same worker retry/mark-failed path as every other
                channel's send failure (``worker.py``'s
                ``except ChannelAdapterError`` -- a bare ``CoreError`` would
                escape that handler entirely).
        """
        org_id = credentials.get("org_id")
        if not org_id:
            raise ChannelAdapterError("send_outbound requires credentials['org_id']")

        try:
            result = await send_email(
                org_id,
                ServiceType.OMNICHANNEL,
                to,
                subject=content.subject or "",
                body_html=content.body_html or content.body_text or "",
                body_text=content.body_text,
                attachments=[
                    {"filename": a.filename, "content": a.content, "mime_type": a.content_type}
                    for a in content.attachments
                ]
                or None,
            )
        except CoreError as exc:
            raise ChannelAdapterError(f"email send failed: {exc}") from exc

        return SendResult(
            external_message_id=result.external_message_id, status=result.status.value
        )

    async def interpret_delivery_webhook(
        self, raw_payload: dict[str, Any]
    ) -> list[DeliveryStatusUpdate]:
        """Map a Core email delivery event (bounce/complaint/delivery) to updates.

        ``raw_payload`` is ``{"message_id": str, "status": str}`` -- the shape
        a subscriber would build from Core's ``email.bounced`` /
        ``email.complained`` event details. There is no producer wired up to
        call this with that shape yet (module docstring) -- but the worker
        (§5.6 Step 4) now calls ``interpret_delivery_webhook`` on *every*
        inbound payload for every channel, including this one's own
        ``{"raw_mime": ..., "external_message_id": ...}`` inbound-email
        payloads. Returning ``[]`` for anything that isn't this method's own
        shape keeps that dual-parse safe instead of raising ``KeyError`` on a
        payload built for a different method entirely.
        """
        message_id = raw_payload.get("message_id")
        raw_status = raw_payload.get("status")
        if not isinstance(message_id, str) or not isinstance(raw_status, str):
            return []
        status = _DELIVERY_STATUS_MAP.get(raw_status, "failed")
        return [DeliveryStatusUpdate(external_message_id=message_id, status=status)]


async def _resolve_raw_mime(raw_payload: dict[str, Any]) -> bytes:
    """Get MIME bytes out of ``normalize_inbound``'s payload, whichever shape it's in."""
    if "raw_mime" in raw_payload:
        raw_mime = raw_payload["raw_mime"]
        return base64.b64decode(raw_mime) if isinstance(raw_mime, str) else raw_mime

    s3_key = raw_payload.get("s3_key")
    org_id = raw_payload.get("org_id")
    if not s3_key or not org_id:
        raise ChannelAdapterError(
            "normalize_inbound requires either raw_payload['raw_mime'] or "
            "raw_payload['s3_key'] + raw_payload['org_id']"
        )
    return await download_file(org_id, s3_key)


def _extract_body(mime: MimeMessage) -> tuple[str | None, str, list[InboundAttachment]]:
    """Walk a parsed MIME message for its text body and any attachments."""
    body_text: str | None = None
    content_type = "text/plain"
    attachments: list[InboundAttachment] = []

    if mime.is_multipart():
        for part in mime.walk():
            if part.is_multipart():
                continue
            disposition = str(part.get("Content-Disposition", ""))
            part_content_type = part.get_content_type()
            if "attachment" in disposition:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    attachments.append(
                        InboundAttachment(
                            filename=part.get_filename() or "attachment",
                            content_type=part_content_type,
                            content=payload,
                        )
                    )
            elif part_content_type in ("text/plain", "text/html") and body_text is None:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    body_text = payload.decode(charset, errors="replace")
                    content_type = part_content_type
    else:
        payload = mime.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = mime.get_content_charset() or "utf-8"
            body_text = payload.decode(charset, errors="replace")
            content_type = mime.get_content_type()

    return body_text, content_type, attachments
