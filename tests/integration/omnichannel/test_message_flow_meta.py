"""Integration tests for the Messenger and Instagram end-to-end message flow.

Mirrors ``test_message_flow.py``'s WhatsApp coverage (webhook -> SQS -> worker
-> Postgres persistence, and the outbound mirror) for the two Meta channels
that previously had none -- Messenger and Instagram share the same
``MessengerPlatformAdapter`` machinery, so one parametrized-by-channel test
module covers both leaves rather than duplicating the WhatsApp file twice.
``core.events.publish_event`` / ``core.realtime.publish_update`` are mocked at
the call site, same as the WhatsApp tests; everything else runs against real
moto SQS/Secrets Manager and Postgres.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.omnichannel import handlers, webhooks, worker
from app.services.omnichannel.adapters import messenger as messenger_module
from app.services.omnichannel.exceptions import ConnectionNotFoundError
from app.services.omnichannel.models import ChannelIdentity, Conversation, Message

from .conftest import messenger_message_payload as _message_payload
from .conftest import (
    mock_membership,
    mock_realtime_and_events,
    seed_connection,
    seed_identity_and_conversation,
    seed_secret,
    sign,
)

pytestmark = pytest.mark.integration

# (channel_type, account_id_key, account_id) -- the one thing that actually
# differs between the two leaves (messenger.py::MessengerPlatformAdapter's
# _account_id_key override); everything else about the flow is identical.
_CHANNELS = [
    ("messenger", "page_id", "PAGE123"),
    ("instagram", "ig_id", "IG777"),
]


@pytest.mark.parametrize(("channel_type", "account_id_key", "account_id"), _CHANNELS)
async def test_inbound_flow_creates_identity_conversation_and_message(
    aws: None,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    channel_type: str,
    account_id_key: str,
    account_id: str,
) -> None:
    mock_publish_event, mock_publish_update = mock_realtime_and_events(monkeypatch)
    connection = await seed_connection(session, channel_type=channel_type)
    await seed_secret(
        connection.org_id, connection.credentials_secret_key, {"app_secret": "meta-app-secret"}
    )

    raw_body = json.dumps(_message_payload(page_id=account_id)).encode("utf-8")
    headers = {"X-Hub-Signature-256": sign(raw_body, "meta-app-secret")}
    await webhooks.handle_webhook(session, channel_type, connection.id, raw_body, headers)

    processed = await worker.process_inbound_batch(session)
    assert processed == 1

    identities = (await session.execute(select(ChannelIdentity))).scalars().all()
    assert len(identities) == 1
    assert identities[0].external_id == "USER1"
    assert identities[0].channel_type == channel_type
    # Neither Messenger nor Instagram webhooks carry an inline profile name.
    assert identities[0].display_name is None

    conversations = (await session.execute(select(Conversation))).scalars().all()
    assert len(conversations) == 1
    assert conversations[0].unread_count == 1

    messages = (await session.execute(select(Message))).scalars().all()
    assert len(messages) == 1
    assert messages[0].external_message_id == "m.ABC"
    assert messages[0].direction == "inbound"

    mock_publish_event.assert_called_once()
    assert mock_publish_event.call_args.args[1] == "message.received"
    mock_publish_update.assert_called_once()


@pytest.mark.parametrize(("channel_type", "account_id_key", "account_id"), _CHANNELS)
async def test_inbound_skips_echo_and_receipt_events(
    aws: None,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    channel_type: str,
    account_id_key: str,
    account_id: str,
) -> None:
    """An echo of the page's own outbound, or a delivery/read receipt with no
    ``message`` key, must not create a phantom conversation."""
    mock_realtime_and_events(monkeypatch)
    connection = await seed_connection(session, channel_type=channel_type)
    await seed_secret(connection.org_id, connection.credentials_secret_key, {"app_secret": "s"})

    payload = {
        "entry": [
            {
                "id": account_id,
                "messaging": [
                    {
                        "sender": {"id": account_id},
                        "message": {"mid": "echo.1", "text": "auto-reply", "is_echo": True},
                    },
                    {"sender": {"id": "USER1"}, "delivery": {"mids": ["m.x"], "watermark": 1}},
                ],
            }
        ]
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = {"X-Hub-Signature-256": sign(raw_body, "s")}
    await webhooks.handle_webhook(session, channel_type, connection.id, raw_body, headers)

    processed = await worker.process_inbound_batch(session)
    assert processed == 1

    assert (await session.execute(select(Conversation))).scalars().all() == []
    assert (await session.execute(select(Message))).scalars().all() == []


@pytest.mark.parametrize(("channel_type", "account_id_key", "account_id"), _CHANNELS)
async def test_outbound_flow_sends_via_the_right_account_id(
    aws: None,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    channel_type: str,
    account_id_key: str,
    account_id: str,
) -> None:
    mock_publish_event, mock_publish_update = mock_realtime_and_events(monkeypatch)
    connection = await seed_connection(session, channel_type=channel_type)
    await seed_secret(
        connection.org_id,
        connection.credentials_secret_key,
        {"app_secret": "s", "page_access_token": "tok", account_id_key: account_id},
    )
    conversation = await seed_identity_and_conversation(session, connection.org_id, channel_type)
    mock_membership(monkeypatch, connection.org_id)

    mock_post = AsyncMock(return_value={"recipient_id": "USER1", "message_id": "mid.OUT1"})
    monkeypatch.setattr(messenger_module, "_post_graph_api", mock_post)

    message, created = await handlers.send_reply(
        session, connection.org_id, conversation.id, "u1", "On its way!"
    )
    assert created is True

    processed = await worker.process_outbound_batch(session)
    assert processed == 1

    await session.refresh(message)
    assert message.status == "sent"
    assert message.external_message_id == "mid.OUT1"

    url, _headers, payload = mock_post.call_args.args
    assert url.endswith(f"/{account_id}/messages")
    assert payload["recipient"]["id"] == "15551234567"
    mock_publish_event.assert_called_once()
    assert mock_publish_event.call_args.args[1] == "message.sent"
    mock_publish_update.assert_called_once()


@pytest.mark.parametrize(("channel_type", "account_id_key", "account_id"), _CHANNELS)
async def test_webhook_channel_type_mismatch_raises(
    aws: None,
    session: AsyncSession,
    channel_type: str,
    account_id_key: str,
    account_id: str,
) -> None:
    """A Messenger connection's URL must reject an Instagram-labeled payload
    and vice versa -- the two leaves share code but not connection identity."""
    other_channel = "instagram" if channel_type == "messenger" else "messenger"
    connection = await seed_connection(session, channel_type=other_channel)
    with pytest.raises(ConnectionNotFoundError):
        await webhooks.handle_webhook(session, channel_type, connection.id, b"{}", {})
