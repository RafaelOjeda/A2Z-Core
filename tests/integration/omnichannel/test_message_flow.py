"""Integration tests for the end-to-end message flow (§5.6, Build Order Step 5).

Exercises the real pipeline: webhook dispatch -> SQS (moto) -> worker ->
Postgres persistence, plus the outbound mirror: handler -> SQS -> worker ->
adapter send. ``core.events.publish_event`` and ``core.realtime.publish_update``
are mocked at the call site (not re-verified here -- Core's own suite already
covers them); everything else -- signature verification, SQS enqueue/receive/
delete, Postgres writes, S3 uploads for attachments -- runs against real
moto/Postgres, not mocks.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SQS_MAX_RECEIVE_COUNT
from app.core.email import EmailResult, EmailStatus
from app.core.exceptions import SuppressionListError
from app.core.membership import Membership, Role
from app.core.storage import upload_file
from app.services.omnichannel import handlers, queues, webhooks, worker
from app.services.omnichannel.adapters import email as email_adapter_module
from app.services.omnichannel.adapters import whatsapp as whatsapp_module
from app.services.omnichannel.exceptions import (
    ChannelAdapterError,
    ConnectionNotFoundError,
    ConversationNotFoundError,
    WebhookSignatureError,
)
from app.services.omnichannel.handlers import ForbiddenError
from app.services.omnichannel.models import (
    ChannelConnection,
    ChannelIdentity,
    Conversation,
    Message,
    MessageAttachment,
)

from .conftest import APP_SECRET as _APP_SECRET
from .conftest import member_stub as _member_stub
from .conftest import mock_realtime_and_events as _mock_realtime_and_events
from .conftest import seed_connection as _seed_connection
from .conftest import seed_identity_and_conversation as _seed_identity_and_conversation
from .conftest import seed_secret as _seed_secret
from .conftest import sign as _sign
from .conftest import whatsapp_message_payload

pytestmark = pytest.mark.integration


def _whatsapp_payload(
    from_number: str = "15551234567", text: str = "Hi there", wamid: str = "wamid.ABC"
) -> bytes:
    return json.dumps(whatsapp_message_payload(from_number, text, wamid)).encode("utf-8")


# --- Inbound: webhook -> SQS -> worker -> persistence ---


async def test_inbound_flow_creates_identity_conversation_and_message(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_publish_event, mock_publish_update = _mock_realtime_and_events(monkeypatch)

    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id, connection.credentials_secret_key, {"app_secret": _APP_SECRET}
    )

    raw_body = _whatsapp_payload()
    headers = {"X-Hub-Signature-256": _sign(raw_body)}
    await webhooks.handle_webhook(session, "whatsapp", connection.id, raw_body, headers)

    processed = await worker.process_inbound_batch(session)
    assert processed == 1

    identities = (await session.execute(select(ChannelIdentity))).scalars().all()
    assert len(identities) == 1
    assert identities[0].external_id == "15551234567"
    assert identities[0].display_name == "Jane"

    conversations = (await session.execute(select(Conversation))).scalars().all()
    assert len(conversations) == 1
    assert conversations[0].unread_count == 1
    assert conversations[0].assigned_user_id is None  # routing is Step 6

    messages = (await session.execute(select(Message))).scalars().all()
    assert len(messages) == 1
    assert messages[0].external_message_id == "wamid.ABC"
    assert messages[0].direction == "inbound"
    assert messages[0].status == "received"

    mock_publish_event.assert_called_once()
    assert mock_publish_event.call_args.args[0] == connection.org_id
    assert mock_publish_event.call_args.args[1] == "message.received"
    mock_publish_update.assert_called_once()


async def test_webhook_signature_rejected(aws: None, session: AsyncSession) -> None:
    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id, connection.credentials_secret_key, {"app_secret": _APP_SECRET}
    )

    raw_body = _whatsapp_payload()
    with pytest.raises(WebhookSignatureError):
        await webhooks.handle_webhook(
            session, "whatsapp", connection.id, raw_body, {"X-Hub-Signature-256": "sha256=bad"}
        )


async def test_webhook_unknown_connection_raises(aws: None, session: AsyncSession) -> None:
    with pytest.raises(ConnectionNotFoundError):
        await webhooks.handle_webhook(session, "whatsapp", "does-not-exist", b"{}", {})


async def test_webhook_channel_type_mismatch_raises(aws: None, session: AsyncSession) -> None:
    connection = await _seed_connection(session, channel_type="email")
    with pytest.raises(ConnectionNotFoundError):
        await webhooks.handle_webhook(session, "whatsapp", connection.id, b"{}", {})


async def test_webhook_retry_produces_one_message_row(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Meta retries a webhook call until it gets a 2xx -- simulate two deliveries."""
    _mock_realtime_and_events(monkeypatch)

    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id, connection.credentials_secret_key, {"app_secret": _APP_SECRET}
    )

    raw_body = _whatsapp_payload()
    headers = {"X-Hub-Signature-256": _sign(raw_body)}
    await webhooks.handle_webhook(session, "whatsapp", connection.id, raw_body, headers)
    await webhooks.handle_webhook(session, "whatsapp", connection.id, raw_body, headers)

    processed = await worker.process_inbound_batch(session, max_messages=10)
    assert processed == 2  # two SQS messages consumed

    messages = (await session.execute(select(Message))).scalars().all()
    assert len(messages) == 1  # but the idempotency constraint held: one row


# --- Inbound email: S3-fetched MIME -> worker -> persistence (§5.2 Step 5) ---


async def test_inbound_email_flow_fetches_mime_from_s3(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production shape for inbound email: SES's receipt pipeline can't
    put MIME bytes on an SQS message body, so it writes to S3 and the
    adapter fetches by key (adapters/email.py::_resolve_raw_mime). Exercises
    the real path -- moto S3 upload, then the worker's normal
    normalize_inbound -> persist -> attachment-upload flow, no mocking of
    the adapter itself."""
    _mock_realtime_and_events(monkeypatch)
    connection = await _seed_connection(session, channel_type="email")

    mime = MIMEMultipart("mixed")
    mime["From"] = "customer@example.com"
    mime["Subject"] = "Question about my order"
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Is this in stock?", "plain"))
    mime.attach(alt)
    part = MIMEApplication(b"screenshot-bytes")
    part.add_header("Content-Disposition", "attachment", filename="screenshot.png")
    mime.attach(part)

    stored = await upload_file(
        connection.org_id, "omnichannel", "raw.eml", mime.as_bytes(), "message/rfc822", "system"
    )

    await queues.enqueue_inbound(
        org_id=connection.org_id,
        channel_type="email",
        connection_id=connection.id,
        raw_payload={
            "s3_key": stored.key,
            "org_id": connection.org_id,
            "external_message_id": "ses-msg-1",
        },
    )
    processed = await worker.process_inbound_batch(session)
    assert processed == 1

    identities = (await session.execute(select(ChannelIdentity))).scalars().all()
    assert len(identities) == 1
    assert identities[0].external_id == "customer@example.com"
    assert identities[0].channel_type == "email"

    messages = (await session.execute(select(Message))).scalars().all()
    assert len(messages) == 1
    assert messages[0].external_message_id == "ses-msg-1"
    assert messages[0].body_text == "Is this in stock?"

    attachments = (await session.execute(select(MessageAttachment))).scalars().all()
    assert len(attachments) == 1
    assert attachments[0].content_type == "application/octet-stream"
    assert attachments[0].size_bytes == len(b"screenshot-bytes")


async def test_inbound_email_duplicate_delivery_is_idempotent(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_realtime_and_events(monkeypatch)
    connection = await _seed_connection(session, channel_type="email")

    mime = MIMEText("hello again")
    mime["From"] = "customer@example.com"
    stored = await upload_file(
        connection.org_id, "omnichannel", "raw.eml", mime.as_bytes(), "message/rfc822", "system"
    )
    raw_payload = {
        "s3_key": stored.key,
        "org_id": connection.org_id,
        "external_message_id": "ses-msg-dup",
    }

    await queues.enqueue_inbound(
        org_id=connection.org_id,
        channel_type="email",
        connection_id=connection.id,
        raw_payload=raw_payload,
    )
    await queues.enqueue_inbound(
        org_id=connection.org_id,
        channel_type="email",
        connection_id=connection.id,
        raw_payload=raw_payload,
    )
    processed = await worker.process_inbound_batch(session, max_messages=10)
    assert processed == 2

    messages = (await session.execute(select(Message))).scalars().all()
    assert len(messages) == 1


# --- Webhook subscription verification (GET handshake) ---


async def test_verify_subscription_echoes_challenge_on_match(
    aws: None, session: AsyncSession
) -> None:
    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id,
        connection.credentials_secret_key,
        {"app_secret": _APP_SECRET, "verify_token": "let-me-in"},
    )

    challenge = await webhooks.verify_subscription(
        session,
        "whatsapp",
        connection.id,
        {"hub.mode": "subscribe", "hub.verify_token": "let-me-in", "hub.challenge": "12345"},
    )

    assert challenge == "12345"


async def test_verify_subscription_rejects_wrong_token(aws: None, session: AsyncSession) -> None:
    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id,
        connection.credentials_secret_key,
        {"app_secret": _APP_SECRET, "verify_token": "let-me-in"},
    )

    with pytest.raises(ChannelAdapterError):
        await webhooks.verify_subscription(
            session,
            "whatsapp",
            connection.id,
            {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
        )


async def test_verify_subscription_unknown_connection_raises(
    aws: None, session: AsyncSession
) -> None:
    with pytest.raises(ConnectionNotFoundError):
        await webhooks.verify_subscription(session, "whatsapp", "does-not-exist", {})


async def test_verify_subscription_not_supported_for_email(
    aws: None, session: AsyncSession
) -> None:
    connection = await _seed_connection(session, channel_type="email")
    await _seed_secret(connection.org_id, connection.credentials_secret_key, {})

    with pytest.raises(ChannelAdapterError):
        await webhooks.verify_subscription(session, "email", connection.id, {})


# --- Outbound: handler -> SQS -> worker -> adapter send ---


async def test_outbound_flow_sends_and_marks_sent(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_publish_event, mock_publish_update = _mock_realtime_and_events(monkeypatch)

    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id,
        connection.credentials_secret_key,
        {"app_secret": _APP_SECRET, "access_token": "tok", "phone_number_id": "123"},
    )
    conversation = await _seed_identity_and_conversation(session, connection.org_id)

    mock_post = AsyncMock(return_value={"messages": [{"id": "wamid.OUT1"}]})
    monkeypatch.setattr(whatsapp_module, "_post_graph_api", mock_post)
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership",
        AsyncMock(return_value=_member_stub(connection.org_id)),
    )

    message, created = await handlers.send_reply(
        session, connection.org_id, conversation.id, "u1", "On its way!"
    )
    assert created is True
    assert message.status == "queued"

    processed = await worker.process_outbound_batch(session)
    assert processed == 1

    await session.refresh(message)
    assert message.status == "sent"
    assert message.external_message_id == "wamid.OUT1"
    mock_publish_event.assert_called_once()
    assert mock_publish_event.call_args.args[1] == "message.sent"
    mock_publish_update.assert_called_once()


async def test_outbound_prefers_active_connection_deterministically(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two connections on the same org/channel: a disabled one must never be
    picked for outbound (webhooks.py already enforces this for inbound), and
    the choice must be deterministic -- oldest-active-wins -- not whatever
    order Postgres happens to return (worker.py::_find_connection)."""
    disabled = await _seed_connection(session)
    disabled.status = "disabled"
    disabled.credentials_secret_key = "whatsapp-disabled"
    await session.commit()
    await _seed_secret(
        disabled.org_id,
        disabled.credentials_secret_key,
        {"app_secret": _APP_SECRET, "access_token": "wrong-tok", "phone_number_id": "999"},
    )

    active = ChannelConnection(
        org_id=disabled.org_id,
        channel_type="whatsapp",
        display_name="Active WhatsApp",
        provider_account_id="15550002222",
        credentials_secret_key="whatsapp-active",
        status="active",
    )
    session.add(active)
    await session.commit()
    await _seed_secret(
        active.org_id,
        active.credentials_secret_key,
        {"app_secret": _APP_SECRET, "access_token": "right-tok", "phone_number_id": "123"},
    )

    conversation = await _seed_identity_and_conversation(session, disabled.org_id)
    mock_post = AsyncMock(return_value={"messages": [{"id": "wamid.OUT1"}]})
    monkeypatch.setattr(whatsapp_module, "_post_graph_api", mock_post)
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership",
        AsyncMock(return_value=_member_stub(disabled.org_id)),
    )

    await handlers.send_reply(session, disabled.org_id, conversation.id, "u1", "hi")
    processed = await worker.process_outbound_batch(session)
    assert processed == 1

    _url, headers, _payload = mock_post.call_args.args
    assert headers["Authorization"] == "Bearer right-tok"


async def test_email_outbound_failure_follows_mark_failed_path(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CoreError from core.email.send_email (suppression, over-limit, ...)
    must be wrapped as ChannelAdapterError by the email adapter so it follows
    the normal retry/mark-failed path instead of escaping
    _process_outbound_message unhandled (the bug this hardens against)."""
    connection = await _seed_connection(session, channel_type="email")
    conversation = await _seed_identity_and_conversation(session, connection.org_id, "email")
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership",
        AsyncMock(return_value=_member_stub(connection.org_id)),
    )
    monkeypatch.setattr(
        email_adapter_module,
        "send_email",
        AsyncMock(side_effect=SuppressionListError("suppressed")),
    )

    message, _created = await handlers.send_reply(
        session, connection.org_id, conversation.id, "u1", "hi"
    )

    # Must not raise -- process_outbound_batch swallows the send failure and
    # leaves the message queued for SQS's own retry, exactly like any other
    # channel's ChannelAdapterError.
    processed = await worker.process_outbound_batch(session)
    assert processed == 0

    await session.refresh(message)
    assert message.status == "queued"


async def test_email_reply_subject_reaches_send_email(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dead plumbing this closes: OutboundContent.subject has always been
    forwarded by EmailAdapter.send_outbound, but nothing upstream ever
    populated it until SendReplyRequest.subject / Message.subject (0004)."""
    connection = await _seed_connection(session, channel_type="email")
    conversation = await _seed_identity_and_conversation(session, connection.org_id, "email")
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership",
        AsyncMock(return_value=_member_stub(connection.org_id)),
    )
    mock_send = AsyncMock(
        return_value=EmailResult(
            message_id="ses-1",
            status=EmailStatus.SENT,
            timestamp=datetime.now(timezone.utc),
            external_message_id="ses-1",
        )
    )
    monkeypatch.setattr(email_adapter_module, "send_email", mock_send)

    message, _created = await handlers.send_reply(
        session, connection.org_id, conversation.id, "u1", "see attached", subject="Your invoice"
    )
    assert message.subject == "Your invoice"

    processed = await worker.process_outbound_batch(session)
    assert processed == 1

    _args, kwargs = mock_send.call_args
    assert kwargs["subject"] == "Your invoice"


async def test_non_email_reply_with_subject_is_harmless(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subject on a non-email channel is stored but never sent -- the
    router/handler stay channel-agnostic rather than rejecting it."""
    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id,
        connection.credentials_secret_key,
        {"app_secret": _APP_SECRET, "access_token": "tok", "phone_number_id": "123"},
    )
    conversation = await _seed_identity_and_conversation(session, connection.org_id)
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership",
        AsyncMock(return_value=_member_stub(connection.org_id)),
    )
    mock_post = AsyncMock(return_value={"messages": [{"id": "wamid.OUT1"}]})
    monkeypatch.setattr(whatsapp_module, "_post_graph_api", mock_post)

    message, _created = await handlers.send_reply(
        session, connection.org_id, conversation.id, "u1", "hi", subject="ignored"
    )
    processed = await worker.process_outbound_batch(session)
    assert processed == 1

    await session.refresh(message)
    assert message.status == "sent"
    _url, _headers, payload = mock_post.call_args.args
    assert "subject" not in payload  # WhatsApp's send payload has no such field


async def test_send_reply_idempotency_key_replay_does_not_duplicate(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = await _seed_connection(session)
    conversation = await _seed_identity_and_conversation(session, connection.org_id)
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership",
        AsyncMock(return_value=_member_stub(connection.org_id)),
    )

    first, created_first = await handlers.send_reply(
        session, connection.org_id, conversation.id, "u1", "hi", client_dedup_key="req-1"
    )
    second, created_second = await handlers.send_reply(
        session, connection.org_id, conversation.id, "u1", "hi (retried)", client_dedup_key="req-1"
    )

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert second.body_text == "hi"  # the original send, not the retried body

    rows = (await session.execute(select(Message))).scalars().all()
    assert len(rows) == 1


async def test_send_reply_without_idempotency_key_is_not_deduped(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting the header preserves the pre-existing always-a-fresh-send behavior."""
    connection = await _seed_connection(session)
    conversation = await _seed_identity_and_conversation(session, connection.org_id)
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership",
        AsyncMock(return_value=_member_stub(connection.org_id)),
    )

    _first, created_first = await handlers.send_reply(
        session, connection.org_id, conversation.id, "u1", "hi"
    )
    _second, created_second = await handlers.send_reply(
        session, connection.org_id, conversation.id, "u1", "hi"
    )

    assert created_first is True
    assert created_second is True
    rows = (await session.execute(select(Message))).scalars().all()
    assert len(rows) == 2


async def test_send_reply_idempotency_key_scoped_per_conversation(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same client key in a different conversation is not a collision."""
    connection = await _seed_connection(session)
    conv_a = await _seed_identity_and_conversation(session, connection.org_id)
    identity_b = ChannelIdentity(
        org_id=connection.org_id, channel_type="whatsapp", external_id="15559998888"
    )
    session.add(identity_b)
    await session.flush()
    conv_b = Conversation(
        org_id=connection.org_id, customer_identity_id=identity_b.id, status="open"
    )
    session.add(conv_b)
    await session.commit()

    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership",
        AsyncMock(return_value=_member_stub(connection.org_id)),
    )

    _a, created_a = await handlers.send_reply(
        session, connection.org_id, conv_a.id, "u1", "hi a", client_dedup_key="same-key"
    )
    _b, created_b = await handlers.send_reply(
        session, connection.org_id, conv_b.id, "u1", "hi b", client_dedup_key="same-key"
    )

    assert created_a is True
    assert created_b is True


async def test_send_reply_not_a_member_raises(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = await _seed_connection(session)
    conversation = await _seed_identity_and_conversation(session, connection.org_id)
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership", AsyncMock(return_value=None)
    )

    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await handlers.send_reply(session, connection.org_id, conversation.id, "stranger", "hi")


async def test_send_reply_guest_role_forbidden(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = await _seed_connection(session)
    conversation = await _seed_identity_and_conversation(session, connection.org_id)
    guest = Membership(
        user_id="u1",
        org_id=connection.org_id,
        role=Role.GUEST,
        joined_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership", AsyncMock(return_value=guest)
    )

    with pytest.raises(ForbiddenError):
        await handlers.send_reply(session, connection.org_id, conversation.id, "u1", "hi")


async def test_send_reply_unknown_conversation_raises(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership",
        AsyncMock(return_value=_member_stub("org-a")),
    )
    with pytest.raises(ConversationNotFoundError):
        await handlers.send_reply(session, "org-a", "does-not-exist", "u1", "hi")


async def test_outbound_send_failure_leaves_message_for_retry(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id,
        connection.credentials_secret_key,
        {"app_secret": _APP_SECRET, "access_token": "tok", "phone_number_id": "123"},
    )
    conversation = await _seed_identity_and_conversation(session, connection.org_id)
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership",
        AsyncMock(return_value=_member_stub(connection.org_id)),
    )

    request = httpx.Request("POST", "http://x")
    response = httpx.Response(500, request=request)

    async def _raise(*args: object, **kwargs: object) -> dict[str, object]:
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr(whatsapp_module, "_post_graph_api", _raise)

    message, _created = await handlers.send_reply(
        session, connection.org_id, conversation.id, "u1", "hi"
    )

    processed = await worker.process_outbound_batch(session)
    assert processed == 0  # not deleted -- still eligible for SQS's own retry

    await session.refresh(message)
    assert message.status == "queued"  # not marked failed yet -- attempts not exhausted


async def test_outbound_exhausted_attempts_marks_message_failed(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id,
        connection.credentials_secret_key,
        {"app_secret": _APP_SECRET, "access_token": "tok", "phone_number_id": "123"},
    )
    conversation = await _seed_identity_and_conversation(session, connection.org_id)

    message = Message(
        org_id=connection.org_id,
        conversation_id=conversation.id,
        direction="outbound",
        channel_type="whatsapp",
        external_message_id="pending:test",
        body_text="hi",
        content_type="text/plain",
        status="queued",
    )
    session.add(message)
    await session.commit()

    request = httpx.Request("POST", "http://x")
    response = httpx.Response(500, request=request)

    async def _raise(*args: object, **kwargs: object) -> dict[str, object]:
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr(whatsapp_module, "_post_graph_api", _raise)

    # Fabricate a message that's already been redelivered to its max --
    # exercising this without waiting out five real SQS visibility timeouts.
    fake_msg = queues.QueueMessage(
        body={"message_id": message.id},
        attributes={"org_id": connection.org_id},
        receipt_handle="fake-receipt",
        receive_count=SQS_MAX_RECEIVE_COUNT,
    )
    monkeypatch.setattr(queues, "receive_outbound", AsyncMock(return_value=[fake_msg]))
    mock_delete = AsyncMock()
    monkeypatch.setattr(queues, "delete_outbound", mock_delete)

    processed = await worker.process_outbound_batch(session)

    # Marked failed for the UI, but deliberately NOT deleted: deleting an
    # exhausted send would retire it before SQS's redrive policy could move it
    # to the DLQ, leaving the §11 "DLQ depth > 0" alarm permanently unarmed.
    # SQS itself is what retires the message, onto the DLQ.
    assert processed == 0
    mock_delete.assert_not_called()

    await session.refresh(message)
    assert message.status == "failed"
