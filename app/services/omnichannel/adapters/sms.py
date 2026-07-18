"""SMS channel adapter — outbound via AWS SNS SMS.

Provider decision (docs/omnichannel-decisions.md): AWS SNS, not Twilio —
stays AWS-native, reuses ``core.clients.sns()`` directly with no new SDK or
non-AWS credential to manage.

Inbound messages and delivery-status feedback for SNS SMS arrive over an
SNS topic subscription (two-way SMS + delivery-status logging), not a
public HTTP endpoint — structurally the same shape as Core's own SES/SNS
bounce pipeline (``app/lambdas/ses_notifications.py``), not a forgeable
internet webhook like WhatsApp's. The actual SNS-subscribed Lambda is
Step 5 (the end-to-end message flow); this adapter is the channel-specific
translation layer it will call into.

NOTE: the JSON field names below follow AWS's documented two-way-SMS and
delivery-status-logging payload shapes as of this writing. This is the part
of the AWS SMS surface most likely to need a field-name correction once
verified against a live SNS topic — flag that explicitly rather than
silently trusting it (app/services/omnichannel/CLAUDE.md §6.2's
test-harness-note convention).
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from app.core import clients
from app.services.omnichannel.adapters.types import (
    DeliveryStatusUpdate,
    NormalizedInboundMessage,
    OutboundContent,
    SendResult,
    SupportedFeatures,
)
from app.services.omnichannel.exceptions import ChannelAdapterError
from app.services.omnichannel.models import MessageStatus


class SmsAdapter:
    """Channel adapter for SMS via AWS SNS. Implements adapters.base.ChannelAdapter."""

    supported_features = SupportedFeatures()

    def __init__(self, org_id: str) -> None:
        self._org_id = org_id

    async def verify_inbound_signature(
        self, raw_body: bytes, headers: dict[str, str], secret: str
    ) -> bool:
        """Always true: inbound SMS arrives via an SNS topic subscription
        (two-way SMS), triggered only by AWS itself — same reasoning as the
        Email adapter, not a public webhook a forger could hit directly.
        """
        return True

    async def verify_subscription(self, params: dict[str, str], credentials: dict[str, Any]) -> str:
        """Always raises: SMS has no webhook-subscription handshake to answer

        (inbound arrives via an SNS topic subscription, not a public
        webhook -- same reasoning as ``verify_inbound_signature`` always
        returning ``True``).
        """
        raise ChannelAdapterError("SMS has no webhook subscription handshake")

    async def normalize_inbound(
        self, raw_payload: dict[str, Any]
    ) -> list[NormalizedInboundMessage]:
        """Parse an AWS two-way-SMS inbound-message notification."""
        from_number = str(raw_payload.get("originationNumber", ""))
        return [
            NormalizedInboundMessage(
                external_id=from_number,
                external_message_id=str(raw_payload.get("inboundMessageId", "")),
                body_text=raw_payload.get("messageBody"),
                content_type="text/plain",
            )
        ]

    async def send_outbound(
        self, to: str, content: OutboundContent, credentials: dict[str, Any]
    ) -> SendResult:
        """Publish via SNS SMS.

        ``credentials`` may carry ``origination_number``/``sender_id`` for
        the org's registered 10DLC number; both are optional — SNS falls
        back to a shared default sender if omitted.
        """
        attributes: dict[str, Any] = {
            "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"},
        }
        if origination := credentials.get("origination_number"):
            attributes["AWS.MM.SMS.OriginationNumber"] = {
                "DataType": "String",
                "StringValue": origination,
            }
        if sender_id := credentials.get("sender_id"):
            attributes["AWS.SNS.SMS.SenderID"] = {"DataType": "String", "StringValue": sender_id}

        try:
            resp = await clients.run_aws(
                clients.sns().publish,
                PhoneNumber=to,
                Message=content.body_text or "",
                MessageAttributes=attributes,
            )
        except ClientError as exc:
            return SendResult(external_message_id="", status=MessageStatus.FAILED, error=str(exc))

        return SendResult(external_message_id=str(resp["MessageId"]), status=MessageStatus.SENT)

    async def interpret_delivery_webhook(
        self, raw_payload: dict[str, Any]
    ) -> list[DeliveryStatusUpdate]:
        """Parse an SNS SMS delivery-status-logging notification."""
        status = raw_payload.get("status")
        message_id = str(raw_payload.get("notification", {}).get("messageId", ""))

        if status == "SUCCESS":
            return [
                DeliveryStatusUpdate(external_message_id=message_id, status=MessageStatus.DELIVERED)
            ]
        if status == "FAILURE":
            error = raw_payload.get("delivery", {}).get("providerResponse")
            return [
                DeliveryStatusUpdate(
                    external_message_id=message_id, status=MessageStatus.FAILED, error=error
                )
            ]
        return []
