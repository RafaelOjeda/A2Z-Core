"""Facebook Messenger channel adapter -- Meta Messenger Platform (Graph API).

CLAUDE.md §5.2, §15 ("Instagram DM / Messenger ... two more adapter files,
nothing else changes"). Signature verification and the subscription handshake
are inherited from ``MetaGraphAdapter`` (``_meta.py``); this module supplies
the Messenger-Platform payload shapes.

Instagram DMs run on the *same* Messenger Platform, so the actual normalize /
send / delivery logic lives on ``MessengerPlatformAdapter`` and Instagram
subclasses it (``instagram.py``) -- differing only in which credential key
holds the sending account id. ``MessengerAdapter`` is the Facebook-Pages leaf.

v1 scope (identical to WhatsApp, deliberately):

- **Text-only outbound**, ``messaging_type="RESPONSE"`` -- a reply inside the
  24-hour standard-messaging window. Business-initiated sends outside it need
  message tags / one-time notifications, deferred with templates (§15). So
  ``send_outbound`` raises without ``body_text``.
- **Inbound media recorded, not downloaded.** Attachment events carry only a
  URL/id; fetching bytes needs a second credentialed call the Protocol can't
  make, so they persist with a placeholder body (customer message stays
  visible, idempotency holds) -- the same accepted gap as WhatsApp.
- **No inline sender name.** Unlike WhatsApp's ``contacts[]``, Messenger
  webhooks don't carry the sender's profile name; resolving it needs a Graph
  user-profile call, so the identity is created with ``display_name=None``.

Credentials (``app_secret``, ``verify_token``, ``page_access_token``,
``page_id``) are per-org via ``core.secrets`` (§6.2); the caller resolves the
bundle and passes it through, same ``org_id``-in-credentials convention as
``email.py``.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.services.omnichannel.adapters._meta import (
    GRAPH_API_BASE,
    MetaGraphAdapter,
)
from app.services.omnichannel.adapters._meta import (
    post_graph_api as _post_graph_api,
)
from app.services.omnichannel.adapters.types import (
    DeliveryStatusUpdate,
    NormalizedInboundMessage,
    OutboundContent,
    SendResult,
    SupportedFeatures,
)
from app.services.omnichannel.exceptions import ChannelAdapterError


class MessengerPlatformAdapter(MetaGraphAdapter):
    """Shared Messenger-Platform implementation (Messenger + Instagram DMs).

    Leaf adapters override ``_account_id_key`` (the credential holding the
    Send API account id) and, if they wish, the ``supported_features`` /
    docstrings. Everything else -- the ``entry[].messaging[]`` normalize, the
    ``recipient``/``message`` send body, delivery interpretation -- is shared.
    """

    # Which credential key holds the account id for this product's Send API
    # path (/{account_id}/messages). Messenger uses the Page id; Instagram
    # overrides this to the IG professional-account id.
    _account_id_key: str = "page_id"

    supported_features = SupportedFeatures(
        templates=False,
        rich_media=False,
        typing_indicators=False,
        read_receipts=True,
        requires_credentials=True,
    )

    async def normalize_inbound(
        self, raw_payload: dict[str, Any]
    ) -> list[NormalizedInboundMessage]:
        """Flatten ``entry[].messaging[]`` into normalized customer messages.

        The Messenger Platform mixes *everything* into one ``messaging[]``
        array -- customer messages, delivery receipts, read receipts, and
        echoes of the page's own outbound (``message.is_echo``). Only genuine
        inbound customer messages become ``NormalizedInboundMessage``\\ s;
        receipts and echoes are skipped (the worker calls this on every
        payload, so returning them would persist junk conversations).
        """
        messages: list[NormalizedInboundMessage] = []
        for entry in raw_payload.get("entry", []):
            for event in entry.get("messaging", []):
                normalized = _normalize_event(event)
                if normalized is not None:
                    messages.append(normalized)
        return messages

    async def send_outbound(
        self, to: str, content: OutboundContent, credentials: dict[str, Any]
    ) -> SendResult:
        """Send a text message via the Send API ``/{account_id}/messages``.

        ``credentials`` must contain ``org_id`` (convention shared with
        ``email.py``), ``page_access_token``, and the account id under this
        adapter's ``_account_id_key`` -- the caller resolves these via
        ``core.secrets.get_secret`` before calling.
        """
        access_token = credentials.get("page_access_token")
        account_id = credentials.get(self._account_id_key)
        if not credentials.get("org_id") or not access_token or not account_id:
            raise ChannelAdapterError(
                "send_outbound requires org_id, page_access_token, and "
                f"{self._account_id_key} in credentials"
            )
        if not content.body_text:
            raise ChannelAdapterError(
                "Messenger v1 supports text-only outbound (message tags deferred, §15); "
                "content.body_text is required"
            )

        payload = {
            "messaging_type": "RESPONSE",
            "recipient": {"id": to},
            "message": {"text": content.body_text},
        }
        url = f"{GRAPH_API_BASE}/{account_id}/messages"
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            data = await _post_graph_api(url, headers, payload)
        except httpx.HTTPError as exc:
            raise ChannelAdapterError(f"Messenger send failed: {exc}") from exc

        message_id: str = data["message_id"]
        return SendResult(external_message_id=message_id, status="sent")

    async def interpret_delivery_webhook(
        self, raw_payload: dict[str, Any]
    ) -> list[DeliveryStatusUpdate]:
        """Map Messenger-Platform ``delivery`` events to ``delivered`` updates.

        A ``delivery`` event names the specific message ids it covers
        (``delivery.mids[]``), so each maps cleanly to a
        ``DeliveryStatusUpdate``. A ``read`` event is *watermark-only* -- it
        acknowledges every message up to a timestamp, not specific ids -- so
        it can't be mapped to an ``external_message_id`` and is intentionally
        not emitted here. (As for every channel today, this method is not yet
        consumed by the worker -- a pre-existing cross-channel gap.)
        """
        updates: list[DeliveryStatusUpdate] = []
        for entry in raw_payload.get("entry", []):
            for event in entry.get("messaging", []):
                delivery = event.get("delivery")
                if not delivery:
                    continue
                for mid in delivery.get("mids", []):
                    updates.append(
                        DeliveryStatusUpdate(external_message_id=mid, status="delivered")
                    )
        return updates


class MessengerAdapter(MessengerPlatformAdapter):
    """Facebook Messenger (Pages) leaf -- account id is the Page id (default)."""


def _normalize_event(event: dict[str, Any]) -> NormalizedInboundMessage | None:
    """Return a normalized message for a genuine inbound event, else ``None``.

    Skips delivery/read receipts (no ``message`` key) and echoes of the page's
    own outbound (``message.is_echo``) -- neither is a customer message.
    """
    message = event.get("message")
    if not message or message.get("is_echo"):
        return None

    sender = event.get("sender", {})
    external_id = sender.get("id")
    external_message_id = message.get("mid")
    if not external_id or not external_message_id:
        return None

    text = message.get("text")
    if text:
        body_text = text
    else:
        # Attachment message -- bytes need a second credentialed Graph call
        # normalize_inbound can't make; persist a placeholder (accepted v1 gap).
        body_text = "[unsupported message type: attachment]"

    return NormalizedInboundMessage(
        external_id=external_id,
        external_message_id=external_message_id,
        display_name=None,  # Messenger webhooks carry no inline profile name
        body_text=body_text,
        content_type="text/plain",
    )
