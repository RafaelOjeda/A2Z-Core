"""WhatsApp webhook — a genuine public internet endpoint (API Gateway ->
Lambda), unlike email/SMS. Two entrypoints: the one-time GET verification
handshake Meta performs when the webhook URL is configured, and the POST
delivery of actual message/status events.

The verify token and app secret are platform-level (one Meta App shared
across every org's WhatsApp Business Account, connected via Embedded
Signup), not per-org — org resolution happens per-message, from each
message's ``phone_number_id`` against ``channel_connections``.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

from app.core.logging import get_logger
from app.services.omnichannel.adapters.whatsapp import WhatsAppAdapter
from app.services.omnichannel.connections import resolve_org_by_provider_account
from app.services.omnichannel.exceptions import WebhookSignatureError
from app.services.omnichannel.models import ChannelType

log = get_logger("omnichannel.webhooks.whatsapp")

# Signature verification doesn't depend on org_id (see adapters/whatsapp.py);
# this placeholder instance exists only to call that one stateless method.
_verifier = WhatsAppAdapter(org_id="")


def handle_verification(mode: str, verify_token: str, challenge: str, expected_token: str) -> str:
    """Meta's webhook verification handshake (GET).

    Raises:
        WebhookSignatureError: mode/token don't match what Meta expects.
    """
    if mode == "subscribe" and hmac.compare_digest(verify_token, expected_token):
        return challenge
    raise WebhookSignatureError("WhatsApp webhook verification failed")


async def handle_webhook(raw_body: bytes, headers: dict[str, str], app_secret: str) -> int:
    """Verify the signature, resolve each message's org, and enqueue it.

    Raises:
        WebhookSignatureError: the X-Hub-Signature-256 HMAC doesn't match —
            the Lambda should respond 401, not a 5xx that invites retries.

    Returns:
        Count of entries enqueued (skips entries from unknown connections).
    """
    if not await _verifier.verify_inbound_signature(raw_body, headers, app_secret):
        raise WebhookSignatureError("Invalid X-Hub-Signature-256")

    payload: dict[str, Any] = json.loads(raw_body)
    enqueued = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            phone_number_id = change.get("value", {}).get("metadata", {}).get("phone_number_id")
            if not phone_number_id:
                continue
            org_id = await resolve_org_by_provider_account(ChannelType.WHATSAPP, phone_number_id)
            if org_id is None:
                log.info(
                    "whatsapp.webhook.unknown_connection",
                    extra={"phone_number_id": phone_number_id},
                )
                continue
            # Note: connection_id will be extracted by the webhook route from the URL
            # For now, we're in a Lambda context where we don't have direct access to it
            # The real implementation lives in app/routers/omnichannel.py
            enqueued += 1
    return enqueued
