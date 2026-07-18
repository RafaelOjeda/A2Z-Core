"""WhatsApp channel adapter -- Meta WhatsApp Cloud (Graph) API over httpx.

CLAUDE.md §5.2, §13 Step 4.

v1 scope is deliberately narrow, matching the plan's minimal-scope revision:

- **Text-only outbound.** Templates are deferred (§15), and WhatsApp requires
  an approved template to business-initiate a conversation outside the
  24-hour customer-service window -- so v1 WhatsApp is reply-within-24h,
  text-only. ``send_outbound`` raises if there's no ``body_text``.
- **Inbound media is recorded, not downloaded.** Non-text messages (image,
  document, audio, video, location, ...) carry only a Graph API *media id* in
  the webhook payload; fetching the bytes needs a second, credentialed Graph
  API call. ``normalize_inbound``'s Protocol signature takes no credentials,
  so v1 persists these messages with a placeholder body (so the customer's
  message still shows up and idempotency still holds) instead of silently
  dropping them. This is an accepted v1 gap, the same shape as the templates
  deferral -- not an oversight.

Credentials (access token, phone-number id, app secret) are per-org via
``core.secrets`` (§6.2); this adapter never calls ``core.secrets`` itself --
callers resolve the secret and pass it through ``credentials``, same
org_id-in-credentials convention as ``email.py``.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from app.services.omnichannel.adapters.types import (
    DeliveryStatusUpdate,
    NormalizedInboundMessage,
    OutboundContent,
    SendResult,
    SupportedFeatures,
)
from app.services.omnichannel.exceptions import ChannelAdapterError

_GRAPH_API_BASE = "https://graph.facebook.com/v20.0"
_HTTP_TIMEOUT = 10.0


class WhatsAppAdapter:
    """Adapts the Meta WhatsApp Cloud API to the ``ChannelAdapter`` Protocol (§5.2, §7)."""

    # templates=False / rich_media=False are v1 scope, not platform limits --
    # see module docstring. read_receipts=True: Meta's status webhook reports
    # sent/delivered/read natively (interpret_delivery_webhook below).
    supported_features = SupportedFeatures(
        templates=False, rich_media=False, typing_indicators=False, read_receipts=True
    )

    async def verify_inbound_signature(
        self, raw_body: bytes, headers: dict[str, str], secret: str
    ) -> bool:
        """Verify Meta's ``X-Hub-Signature-256`` HMAC-SHA256 over the raw body."""
        received = _get_header(headers, "X-Hub-Signature-256")
        if not received:
            return False
        expected = _compute_signature(raw_body, secret)
        return hmac.compare_digest(received, expected)

    async def verify_subscription(self, params: dict[str, str], credentials: dict[str, Any]) -> str:
        """Answer Meta's webhook-subscription handshake.

        Meta calls ``GET`` with ``hub.mode=subscribe``, ``hub.verify_token``
        (compared against the ``verify_token`` set alongside ``app_secret``
        in this connection's secret, when the webhook URL is registered in
        the Meta App dashboard), and ``hub.challenge`` -- echoed back
        verbatim as plain text on a match, the one part of this handshake
        Meta's docs are strict about.
        """
        expected_token = credentials.get("verify_token")
        challenge = params.get("hub.challenge")
        if (
            params.get("hub.mode") != "subscribe"
            or not expected_token
            or params.get("hub.verify_token") != expected_token
            or not challenge
        ):
            raise ChannelAdapterError("WhatsApp webhook subscription verification failed")
        return challenge

    async def normalize_inbound(
        self, raw_payload: dict[str, Any]
    ) -> list[NormalizedInboundMessage]:
        """Flatten Meta's nested webhook batch into normalized messages.

        Shape: ``entry[].changes[].value.{messages[], contacts[]}`` -- Meta
        batches multiple messages (and contacts) per webhook call.
        """
        messages: list[NormalizedInboundMessage] = []
        for entry in raw_payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                contacts = {
                    contact["wa_id"]: contact.get("profile", {}).get("name")
                    for contact in value.get("contacts", [])
                }
                for msg in value.get("messages", []):
                    messages.append(_normalize_message(msg, contacts))
        return messages

    async def send_outbound(
        self, to: str, content: OutboundContent, credentials: dict[str, Any]
    ) -> SendResult:
        """Send a text message via the Graph API ``/messages`` endpoint.

        ``credentials`` must contain ``org_id`` (convention shared with
        ``email.py``), ``access_token``, and ``phone_number_id`` -- the
        caller resolves these via ``core.secrets.get_secret`` before calling.
        """
        access_token = credentials.get("access_token")
        phone_number_id = credentials.get("phone_number_id")
        if not credentials.get("org_id") or not access_token or not phone_number_id:
            raise ChannelAdapterError(
                "send_outbound requires org_id, access_token, and phone_number_id in credentials"
            )
        if not content.body_text:
            raise ChannelAdapterError(
                "WhatsApp v1 supports text-only outbound (templates deferred, §15); "
                "content.body_text is required"
            )

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": content.body_text},
        }
        url = f"{_GRAPH_API_BASE}/{phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            data = await _post_graph_api(url, headers, payload)
        except httpx.HTTPError as exc:
            raise ChannelAdapterError(f"WhatsApp send failed: {exc}") from exc

        message_id: str = data["messages"][0]["id"]
        return SendResult(external_message_id=message_id, status="sent")

    async def interpret_delivery_webhook(
        self, raw_payload: dict[str, Any]
    ) -> list[DeliveryStatusUpdate]:
        """Map Meta's status webhook (``sent``/``delivered``/``read``/``failed``) through.

        Unlike email (which folds bounce/complaint into "failed"), WhatsApp's
        own status vocabulary already matches ``Message.status`` in
        ``models.py`` (§5.1), so no remapping is needed.
        """
        updates: list[DeliveryStatusUpdate] = []
        for entry in raw_payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for status in value.get("statuses", []):
                    updates.append(
                        DeliveryStatusUpdate(
                            external_message_id=status["id"], status=status["status"]
                        )
                    )
        return updates


def _normalize_message(
    msg: dict[str, Any], contacts: dict[str, str | None]
) -> NormalizedInboundMessage:
    from_number: str = msg["from"]
    external_message_id: str = msg["id"]
    msg_type = msg.get("type", "unknown")

    if msg_type == "text":
        body_text = msg.get("text", {}).get("body")
    else:
        # Accepted v1 gap -- see module docstring: media bytes need a second
        # credentialed Graph API call that normalize_inbound can't make.
        body_text = f"[unsupported message type: {msg_type}]"

    return NormalizedInboundMessage(
        external_id=from_number,
        external_message_id=external_message_id,
        display_name=contacts.get(from_number),
        body_text=body_text,
        content_type="text/plain",
    )


def _compute_signature(raw_body: bytes, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _get_header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


async def _post_graph_api(
    url: str, headers: dict[str, str], payload: dict[str, Any]
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data
