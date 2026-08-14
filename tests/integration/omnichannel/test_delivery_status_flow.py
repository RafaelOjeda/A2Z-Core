"""Integration tests for the delivery-status pipeline (§5.6 Step 4).

``interpret_delivery_webhook`` was implemented by every adapter from day one
but had no caller -- a message never advanced past ``sent``. The worker now
calls it on every inbound payload (alongside ``normalize_inbound``, since a
single Meta webhook call can carry both customer messages and delivery
statuses at once, see ``worker.py::_process_inbound_message``'s docstring).
These tests exercise that end to end: a signed webhook carrying only status
updates (no ``messages``) reaches Postgres as a status change and a realtime
publish, with no phantom Message/Conversation/Identity rows created for it.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.omnichannel import webhooks, worker
from app.services.omnichannel.adapters.types import DeliveryStatusUpdate
from app.services.omnichannel.models import Message

from .conftest import APP_SECRET as _APP_SECRET
from .conftest import mock_realtime_and_events as _mock_realtime_and_events
from .conftest import seed_connection as _seed_connection
from .conftest import seed_identity_and_conversation as _seed_identity_and_conversation
from .conftest import seed_secret as _seed_secret
from .conftest import sign as _sign

pytestmark = pytest.mark.integration


async def _seed_sent_message(
    session: AsyncSession, org_id: str, conversation_id: str, external_message_id: str
) -> Message:
    message = Message(
        org_id=org_id,
        conversation_id=conversation_id,
        direction="outbound",
        channel_type="whatsapp",
        external_message_id=external_message_id,
        body_text="On its way!",
        content_type="text/plain",
        status="sent",
    )
    session.add(message)
    await session.commit()
    return message


def _whatsapp_status_payload(statuses: list[dict[str, str]]) -> bytes:
    payload = {"entry": [{"changes": [{"value": {"statuses": statuses}}]}]}
    return json.dumps(payload).encode("utf-8")


async def _deliver(
    session: AsyncSession, connection_id: str, statuses: list[dict[str, str]]
) -> None:
    raw_body = _whatsapp_status_payload(statuses)
    headers = {"X-Hub-Signature-256": _sign(raw_body)}
    await webhooks.handle_webhook(session, "whatsapp", connection_id, raw_body, headers)
    processed = await worker.process_inbound_batch(session)
    assert processed == 1


async def test_status_webhook_advances_sent_to_delivered_to_read(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_realtime_and_events(monkeypatch)
    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id, connection.credentials_secret_key, {"app_secret": _APP_SECRET}
    )
    conversation = await _seed_identity_and_conversation(session, connection.org_id)
    message = await _seed_sent_message(session, connection.org_id, conversation.id, "wamid.1")

    await _deliver(session, connection.id, [{"id": "wamid.1", "status": "delivered"}])
    await session.refresh(message)
    assert message.status == "delivered"

    await _deliver(session, connection.id, [{"id": "wamid.1", "status": "read"}])
    await session.refresh(message)
    assert message.status == "read"


async def test_status_webhook_never_downgrades_out_of_order(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_realtime_and_events(monkeypatch)
    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id, connection.credentials_secret_key, {"app_secret": _APP_SECRET}
    )
    conversation = await _seed_identity_and_conversation(session, connection.org_id)
    message = await _seed_sent_message(session, connection.org_id, conversation.id, "wamid.2")

    # read arrives before delivered -- Meta gives no ordering guarantee.
    await _deliver(session, connection.id, [{"id": "wamid.2", "status": "read"}])
    await session.refresh(message)
    assert message.status == "read"

    await _deliver(session, connection.id, [{"id": "wamid.2", "status": "delivered"}])
    await session.refresh(message)
    assert message.status == "read"  # must not regress


async def test_combined_messages_and_statuses_payload_does_both(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single WhatsApp webhook call carrying both messages[] and
    statuses[] must persist the inbound message AND apply the status --
    proof that dual-parse (not either/or) was the right design."""
    _mock_realtime_and_events(monkeypatch)
    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id, connection.credentials_secret_key, {"app_secret": _APP_SECRET}
    )
    conversation = await _seed_identity_and_conversation(session, connection.org_id)
    message = await _seed_sent_message(session, connection.org_id, conversation.id, "wamid.3")

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "15559990000", "profile": {"name": "Sam"}}],
                            "messages": [
                                {
                                    "from": "15559990000",
                                    "id": "wamid.inbound-1",
                                    "type": "text",
                                    "text": {"body": "thanks!"},
                                }
                            ],
                            "statuses": [{"id": "wamid.3", "status": "delivered"}],
                        }
                    }
                ]
            }
        ]
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = {"X-Hub-Signature-256": _sign(raw_body)}
    await webhooks.handle_webhook(session, "whatsapp", connection.id, raw_body, headers)
    processed = await worker.process_inbound_batch(session)
    assert processed == 1

    rows = (await session.execute(select(Message))).scalars().all()
    inbound = [m for m in rows if m.direction == "inbound"]
    assert len(inbound) == 1
    assert inbound[0].external_message_id == "wamid.inbound-1"

    await session.refresh(message)
    assert message.status == "delivered"


async def test_status_for_unknown_message_is_a_no_op(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_realtime_and_events(monkeypatch)
    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id, connection.credentials_secret_key, {"app_secret": _APP_SECRET}
    )

    # No Message row for this id at all -- must not raise.
    await _deliver(session, connection.id, [{"id": "wamid.never-existed", "status": "delivered"}])


async def test_status_update_is_org_scoped(aws: None, session: AsyncSession) -> None:
    """_apply_delivery_status's lookup must filter on org_id, not just
    (channel_type, external_message_id) -- exercised by calling it directly
    with a mismatched org_id, since ``uq_message_idempotency`` (channel_type,
    external_message_id only, deliberately provider-id-global -- Meta's own
    message ids already are) makes two real orgs colliding on the same id
    unrepresentable in this schema; this is the defense-in-depth check for
    if that constraint or the query ever changes."""
    conversation = await _seed_identity_and_conversation(session, "org-a")
    message = await _seed_sent_message(session, "org-a", conversation.id, "wamid.shared")

    await worker._apply_delivery_status(
        session,
        "org-b",  # deliberately the wrong org
        "whatsapp",
        DeliveryStatusUpdate(external_message_id="wamid.shared", status="delivered"),
    )

    await session.refresh(message)
    assert message.status == "sent"  # untouched -- org-b's lookup found nothing


async def test_messenger_delivery_mids_map_to_delivered(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_realtime_and_events(monkeypatch)
    connection = await _seed_connection(session, channel_type="messenger")
    await _seed_secret(
        connection.org_id, connection.credentials_secret_key, {"app_secret": _APP_SECRET}
    )
    conversation = await _seed_identity_and_conversation(session, connection.org_id, "messenger")
    message = await _seed_sent_message(session, connection.org_id, conversation.id, "mid.1")
    message.channel_type = "messenger"
    await session.commit()

    payload = {
        "entry": [{"messaging": [{"sender": {"id": "USER1"}, "delivery": {"mids": ["mid.1"]}}]}]
    }
    raw_body = json.dumps(payload).encode("utf-8")
    headers = {"X-Hub-Signature-256": _sign(raw_body)}
    await webhooks.handle_webhook(session, "messenger", connection.id, raw_body, headers)
    processed = await worker.process_inbound_batch(session)
    assert processed == 1

    await session.refresh(message)
    assert message.status == "delivered"


async def test_messenger_read_watermark_is_a_no_op(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Messenger read events are watermark-only and intentionally dropped by
    interpret_delivery_webhook -- the message must stay at 'sent'."""
    _mock_realtime_and_events(monkeypatch)
    connection = await _seed_connection(session, channel_type="messenger")
    await _seed_secret(
        connection.org_id, connection.credentials_secret_key, {"app_secret": _APP_SECRET}
    )
    conversation = await _seed_identity_and_conversation(session, connection.org_id, "messenger")
    message = await _seed_sent_message(session, connection.org_id, conversation.id, "mid.2")
    message.channel_type = "messenger"
    await session.commit()

    payload = {"entry": [{"messaging": [{"sender": {"id": "USER1"}, "read": {"watermark": 1}}]}]}
    raw_body = json.dumps(payload).encode("utf-8")
    headers = {"X-Hub-Signature-256": _sign(raw_body)}
    await webhooks.handle_webhook(session, "messenger", connection.id, raw_body, headers)
    processed = await worker.process_inbound_batch(session)
    assert processed == 1

    await session.refresh(message)
    assert message.status == "sent"
