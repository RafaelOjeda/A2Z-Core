"""Unit tests for the email ChannelAdapter (CLAUDE.md §5.2, §13 Step 3).

Mocks ``core.email.send_email`` directly -- these tests verify the adapter's
own logic (arg mapping, MIME parsing, the org_id convention), not Core's email
plumbing, which already has its own suite.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import AsyncMock

import pytest

from app.core.email import EmailResult, EmailStatus
from app.services.omnichannel.adapters import email as email_adapter_module
from app.services.omnichannel.adapters.email import EmailAdapter
from app.services.omnichannel.adapters.types import OutboundAttachment, OutboundContent
from app.services.omnichannel.exceptions import ChannelAdapterError

adapter = EmailAdapter()


def _fake_result(external_message_id: str = "ses-msg-1") -> EmailResult:
    return EmailResult(
        message_id=external_message_id,
        status=EmailStatus.SENT,
        timestamp=datetime.now(timezone.utc),
        external_message_id=external_message_id,
    )


async def test_send_outbound_calls_core_email(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_send = AsyncMock(return_value=_fake_result())
    monkeypatch.setattr(email_adapter_module, "send_email", mock_send)

    content = OutboundContent(subject="Hi", body_text="hello", body_html="<p>hello</p>")
    result = await adapter.send_outbound("customer@example.com", content, {"org_id": "org-a"})

    assert result.external_message_id == "ses-msg-1"
    assert result.status == "sent"
    mock_send.assert_called_once()
    args, kwargs = mock_send.call_args
    assert args[0] == "org-a"  # org_id
    assert args[2] == "customer@example.com"  # to
    assert kwargs["subject"] == "Hi"
    assert kwargs["body_text"] == "hello"


async def test_send_outbound_passes_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_send = AsyncMock(return_value=_fake_result())
    monkeypatch.setattr(email_adapter_module, "send_email", mock_send)

    content = OutboundContent(
        body_text="see attached",
        attachments=[
            OutboundAttachment(filename="a.pdf", content_type="application/pdf", content=b"%PDF")
        ],
    )
    await adapter.send_outbound("customer@example.com", content, {"org_id": "org-a"})

    _, kwargs = mock_send.call_args
    assert kwargs["attachments"] == [
        {"filename": "a.pdf", "content": b"%PDF", "mime_type": "application/pdf"}
    ]


async def test_send_outbound_requires_org_id(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_send = AsyncMock(return_value=_fake_result())
    monkeypatch.setattr(email_adapter_module, "send_email", mock_send)

    with pytest.raises(ChannelAdapterError):
        await adapter.send_outbound("customer@example.com", OutboundContent(body_text="hi"), {})
    mock_send.assert_not_called()


async def test_verify_inbound_signature_always_true() -> None:
    assert await adapter.verify_inbound_signature(b"", {}, "unused-secret") is True


async def test_normalize_inbound_plain_text() -> None:
    raw = (
        b"From: customer@example.com\r\n"
        b"To: sales@acme.com\r\n"
        b"Subject: Question\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Do you ship internationally?"
    )
    messages = await adapter.normalize_inbound({"raw_mime": raw, "external_message_id": "ses-in-1"})

    assert len(messages) == 1
    msg = messages[0]
    assert msg.external_id == "customer@example.com"
    assert msg.external_message_id == "ses-in-1"
    assert msg.body_text is not None
    assert "ship internationally" in msg.body_text
    assert msg.attachments == []


async def test_normalize_inbound_multipart_with_attachment() -> None:
    mime = MIMEMultipart("mixed")
    mime["From"] = "customer@example.com"
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("plain body", "plain"))
    alt.attach(MIMEText("<p>html body</p>", "html"))
    mime.attach(alt)
    from email.mime.application import MIMEApplication

    part = MIMEApplication(b"binarydata")
    part.add_header("Content-Disposition", "attachment", filename="receipt.pdf")
    mime.attach(part)

    messages = await adapter.normalize_inbound({
        "raw_mime": mime.as_bytes(),
        "external_message_id": "ses-in-2",
    })

    msg = messages[0]
    assert msg.body_text == "plain body"
    assert len(msg.attachments) == 1
    assert msg.attachments[0].filename == "receipt.pdf"
    assert msg.attachments[0].content == b"binarydata"


@pytest.mark.parametrize(
    ("core_status", "expected"),
    [("delivered", "delivered"), ("bounced", "failed"), ("complained", "failed")],
)
async def test_interpret_delivery_webhook_maps_status(core_status: str, expected: str) -> None:
    updates = await adapter.interpret_delivery_webhook({"message_id": "m1", "status": core_status})
    assert len(updates) == 1
    assert updates[0].external_message_id == "m1"
    assert updates[0].status == expected


def test_email_does_not_require_a_stored_credential() -> None:
    """Email authenticates via the org's verified sending domain, not a
    core.secrets-backed connection credential (connections.py's self-service
    branch reads this flag to skip the secret entirely)."""
    assert adapter.supported_features.requires_credentials is False
