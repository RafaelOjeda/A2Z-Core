"""Shared base for Meta Graph API channel adapters (WhatsApp, Messenger, Instagram).

CLAUDE.md §5.2, §7, §15 ("Instagram DM / Messenger ... two more adapter files,
nothing else changes"). All three Meta channels share two pieces of machinery
*exactly*:

- **Inbound signature verification** -- Meta signs every webhook body with
  ``X-Hub-Signature-256`` = ``sha256=<HMAC-SHA256(app_secret, raw_body)>``.
- **The subscription handshake** -- when a webhook URL is registered in the
  Meta App dashboard, Meta calls ``GET`` once with
  ``hub.mode=subscribe`` / ``hub.verify_token`` / ``hub.challenge`` and
  expects the challenge echoed back verbatim.

Those live here so a new Meta channel doesn't re-implement them. Everything
that actually *differs* per channel -- the inbound payload shape, the send
endpoint and body, delivery-status interpretation, and ``supported_features``
-- stays in the leaf adapter (``whatsapp.py``, ``messenger.py``,
``instagram.py``). This class deliberately does **not** implement those three
methods; a leaf that forgets one fails ``isinstance(..., ChannelAdapter)`` (the
registry's Protocol guard) loudly rather than silently.

Credentials (``app_secret``, ``verify_token``, and the channel's access
token) are per-org via ``core.secrets`` (§6.2); adapters never call
``core.secrets`` themselves -- callers resolve the secret bundle and pass it
through, same ``org_id``-in-credentials convention as ``email.py``.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from app.services.omnichannel.exceptions import ChannelAdapterError

# Meta's Graph API version. Bumped in one place for every Meta channel.
GRAPH_API_BASE = "https://graph.facebook.com/v20.0"
_HTTP_TIMEOUT = 10.0


class MetaGraphAdapter:
    """Base for Meta Graph API channels: shared signature + subscription verify.

    Leaf adapters subclass this and add ``supported_features`` plus the three
    channel-specific methods (``normalize_inbound``, ``send_outbound``,
    ``interpret_delivery_webhook``) to satisfy the ``ChannelAdapter`` Protocol.
    """

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
        (compared against the ``verify_token`` stored alongside ``app_secret``
        in this connection's secret when the webhook URL is registered in the
        Meta App dashboard), and ``hub.challenge`` -- echoed back verbatim as
        plain text on a match, the one part of this handshake Meta's docs are
        strict about.

        Raises:
            ChannelAdapterError: The request doesn't check out (wrong/missing
                verify token, or not a ``subscribe`` request).
        """
        expected_token = credentials.get("verify_token")
        challenge = params.get("hub.challenge")
        if (
            params.get("hub.mode") != "subscribe"
            or not expected_token
            or params.get("hub.verify_token") != expected_token
            or not challenge
        ):
            raise ChannelAdapterError("Meta webhook subscription verification failed")
        return challenge


def _compute_signature(raw_body: bytes, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _get_header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


async def post_graph_api(
    url: str, headers: dict[str, str], payload: dict[str, Any]
) -> dict[str, Any]:
    """POST JSON to the Graph API, raising for non-2xx. Shared by every Meta send."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data
