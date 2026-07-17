"""Unit tests for the SMS channel adapter (AWS SNS SMS, §5.2/Step 4)."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from app.core import clients
from app.services.omnichannel.adapters.base import ChannelAdapter
from app.services.omnichannel.adapters.sms import SmsAdapter
from app.services.omnichannel.adapters.types import OutboundContent
from app.services.omnichannel.models import MessageStatus


def test_adapter_satisfies_channel_adapter_protocol() -> None:
    assert isinstance(SmsAdapter("org-1"), ChannelAdapter)


async def test_verify_inbound_signature_is_always_true() -> None:
    adapter = SmsAdapter("org-1")
    assert await adapter.verify_inbound_signature(b"anything", {}, "unused-secret") is True


async def test_normalize_inbound_parses_two_way_sms_payload() -> None:
    adapter = SmsAdapter("org-1")
    payload = {
        "originationNumber": "+15550001111",
        "destinationNumber": "+15559998888",
        "messageBody": "Can I get a quote?",
        "inboundMessageId": "sns-msg-1",
    }

    messages = await adapter.normalize_inbound(payload)

    assert len(messages) == 1
    msg = messages[0]
    # NormalizedInboundMessage is deliberately channel-agnostic (§5.2): the
    # channel and the org's own number come from the connection the webhook
    # arrived on, not the message body -- so the normalized shape carries only
    # the customer's identity, the provider id, and the text.
    assert msg.external_message_id == "sns-msg-1"
    assert msg.external_id == "+15550001111"
    assert msg.body_text == "Can I get a quote?"


async def test_send_outbound_publishes_transactional_sms(monkeypatch: pytest.MonkeyPatch) -> None:
    sns = Mock()
    sns.publish = Mock(return_value={"MessageId": "sns-out-1"})
    monkeypatch.setattr(clients, "sns", lambda: sns)

    adapter = SmsAdapter("org-1")
    result = await adapter.send_outbound(
        "+15550001111", OutboundContent(body_text="Your quote is ready"), credentials={}
    )

    assert result.status == MessageStatus.SENT
    assert result.external_message_id == "sns-out-1"
    kwargs = sns.publish.call_args.kwargs
    assert kwargs["PhoneNumber"] == "+15550001111"
    assert kwargs["Message"] == "Your quote is ready"
    assert kwargs["MessageAttributes"]["AWS.SNS.SMS.SMSType"]["StringValue"] == "Transactional"
    assert "AWS.MM.SMS.OriginationNumber" not in kwargs["MessageAttributes"]


async def test_send_outbound_includes_origination_number_and_sender_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sns = Mock()
    sns.publish = Mock(return_value={"MessageId": "sns-out-2"})
    monkeypatch.setattr(clients, "sns", lambda: sns)

    adapter = SmsAdapter("org-1")
    await adapter.send_outbound(
        "+15550001111",
        OutboundContent(body_text="hi"),
        credentials={"origination_number": "+15551110000", "sender_id": "ACME"},
    )

    attrs = sns.publish.call_args.kwargs["MessageAttributes"]
    assert attrs["AWS.MM.SMS.OriginationNumber"]["StringValue"] == "+15551110000"
    assert attrs["AWS.SNS.SMS.SenderID"]["StringValue"] == "ACME"


async def test_send_outbound_returns_failed_result_on_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sns = Mock()
    sns.publish = Mock(
        side_effect=ClientError(
            {"Error": {"Code": "Throttling", "Message": "slow down"}}, "Publish"
        )
    )
    monkeypatch.setattr(clients, "sns", lambda: sns)

    adapter = SmsAdapter("org-1")
    result = await adapter.send_outbound(
        "+15550001111", OutboundContent(body_text="hi"), credentials={}
    )

    assert result.status == MessageStatus.FAILED
    assert result.error is not None


async def test_interpret_delivery_webhook_success() -> None:
    adapter = SmsAdapter("org-1")
    payload = {"notification": {"messageId": "sns-out-1"}, "status": "SUCCESS"}

    updates = await adapter.interpret_delivery_webhook(payload)

    assert len(updates) == 1
    assert updates[0].external_message_id == "sns-out-1"
    assert updates[0].status == MessageStatus.DELIVERED


async def test_interpret_delivery_webhook_failure() -> None:
    adapter = SmsAdapter("org-1")
    payload = {
        "notification": {"messageId": "sns-out-1"},
        "status": "FAILURE",
        "delivery": {"providerResponse": "Carrier rejected"},
    }

    updates = await adapter.interpret_delivery_webhook(payload)

    assert updates[0].status == MessageStatus.FAILED
    assert updates[0].error == "Carrier rejected"


async def test_interpret_delivery_webhook_ignores_unknown_status() -> None:
    adapter = SmsAdapter("org-1")
    updates = await adapter.interpret_delivery_webhook({"status": "PENDING"})
    assert updates == []
