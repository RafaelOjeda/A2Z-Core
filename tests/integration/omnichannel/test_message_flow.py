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

import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SQS_MAX_RECEIVE_COUNT
from app.core import clients
from app.core.membership import Membership, Role
from app.services.omnichannel import handlers, queues, webhooks, worker
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
)

pytestmark = pytest.mark.integration

_APP_SECRET = "wa-app-secret"


def _sign(raw_body: bytes) -> str:
    mac = hmac.new(_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


async def _seed_secret(org_id: str, key: str, value: dict[str, str]) -> None:
    await clients.run_aws(
        clients.secretsmanager().create_secret,
        Name=f"a2z/{org_id}/omnichannel/{key}",
        SecretString=json.dumps(value),
    )


async def _seed_connection(
    session: AsyncSession, org_id: str = "org-a", channel_type: str = "whatsapp"
) -> ChannelConnection:
    connection = ChannelConnection(
        org_id=org_id,
        channel_type=channel_type,
        display_name="Test WhatsApp",
        provider_account_id="15550001111",
        credentials_secret_key="whatsapp-main",
        status="active",
    )
    session.add(connection)
    await session.commit()
    return connection


def _whatsapp_payload(
    from_number: str = "15551234567", text: str = "Hi there", wamid: str = "wamid.ABC"
) -> bytes:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": from_number, "profile": {"name": "Jane"}}],
                            "messages": [
                                {
                                    "from": from_number,
                                    "id": wamid,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def _mock_realtime_and_events(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    mock_publish_event = AsyncMock()
    mock_publish_update = AsyncMock()
    monkeypatch.setattr(worker, "publish_event", mock_publish_event)
    monkeypatch.setattr(worker, "publish_update", mock_publish_update)
    return mock_publish_event, mock_publish_update


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


async def _seed_identity_and_conversation(
    session: AsyncSession, org_id: str, channel_type: str = "whatsapp"
) -> Conversation:
    identity = ChannelIdentity(org_id=org_id, channel_type=channel_type, external_id="15551234567")
    session.add(identity)
    await session.flush()
    conversation = Conversation(org_id=org_id, customer_identity_id=identity.id, status="open")
    session.add(conversation)
    await session.commit()
    return conversation


def _member_stub(org_id: str) -> Membership:
    return Membership(
        user_id="u1", org_id=org_id, role=Role.MEMBER, joined_at=datetime.now(timezone.utc)
    )


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
