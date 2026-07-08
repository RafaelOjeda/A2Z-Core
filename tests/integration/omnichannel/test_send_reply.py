"""Outbound message flow tests (§5.6, the mirrored half of test_message_flow.py)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import IntegrityError

import app.config as config_module
from app.core import clients
from app.core.email import EmailResult, EmailStatus
from app.core.exceptions import EmailError, RateLimitError
from app.services.omnichannel import db, handlers
from app.services.omnichannel.adapters.types import OutboundContent
from app.services.omnichannel.exceptions import ConversationNotFoundError
from app.services.omnichannel.models import (
    ChannelIdentity,
    ChannelType,
    Conversation,
    ConversationStatus,
)

pytestmark = pytest.mark.integration


class _FakeWhatsAppResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, list[dict[str, str]]]:
        return {"messages": [{"id": "wamid.out"}]}


async def _make_conversation(org_id: str, channel_type: ChannelType, external_id: str) -> uuid.UUID:
    async with db.get_session() as session:
        identity = ChannelIdentity(
            org_id=org_id, channel_type=channel_type.value, external_id=external_id
        )
        session.add(identity)
        await session.flush()
        conversation = Conversation(
            org_id=org_id, customer_identity_id=identity.id, status=ConversationStatus.OPEN
        )
        session.add(conversation)
        await session.flush()
        return conversation.id


async def test_send_reply_persists_sent_message_and_updates_conversation(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_id = await _make_conversation("org-a", ChannelType.EMAIL, "customer@example.com")

    fake_send = AsyncMock(
        return_value=EmailResult(
            message_id="ses-1",
            status=EmailStatus.SENT,
            timestamp=datetime.now(timezone.utc),
            external_message_id="ses-1",
        )
    )
    monkeypatch.setattr("app.services.omnichannel.adapters.email.send_email", fake_send)

    message = await handlers.send_reply(
        "org-a", conversation_id, "user-1", OutboundContent(subject="Hi", body_text="Hello"), {}
    )

    assert message.status.value == "sent"
    assert message.external_message_id == "ses-1"

    async with db.get_session() as session:
        conv = await session.get(Conversation, conversation_id)
        assert conv is not None
        assert conv.last_message_preview == "Hello"
        assert conv.last_message_at is not None


async def test_send_reply_raises_for_unknown_conversation(aws: None) -> None:
    with pytest.raises(ConversationNotFoundError):
        await handlers.send_reply(
            "org-a", uuid.uuid4(), "user-1", OutboundContent(body_text="hi"), {}
        )


async def test_send_reply_raises_when_conversation_belongs_to_another_org(aws: None) -> None:
    conversation_id = await _make_conversation("org-a", ChannelType.EMAIL, "customer@example.com")

    with pytest.raises(ConversationNotFoundError):
        await handlers.send_reply(
            "org-b", conversation_id, "user-1", OutboundContent(body_text="hi"), {}
        )


async def test_send_reply_persists_failed_status_without_raising(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_id = await _make_conversation("org-a", ChannelType.EMAIL, "bad@example.com")

    fake_send = AsyncMock(side_effect=EmailError("suppressed"))
    monkeypatch.setattr("app.services.omnichannel.adapters.email.send_email", fake_send)

    message = await handlers.send_reply(
        "org-a", conversation_id, "user-1", OutboundContent(body_text="hi"), {}
    )

    assert message.status.value == "failed"
    assert message.external_message_id.startswith("failed-")


async def test_send_reply_two_failed_sends_do_not_collide_on_unique_constraint(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_id = await _make_conversation("org-a", ChannelType.EMAIL, "bad@example.com")
    monkeypatch.setattr(
        "app.services.omnichannel.adapters.email.send_email",
        AsyncMock(side_effect=EmailError("suppressed")),
    )

    try:
        await handlers.send_reply(
            "org-a", conversation_id, "user-1", OutboundContent(body_text="a"), {}
        )
        await handlers.send_reply(
            "org-a", conversation_id, "user-1", OutboundContent(body_text="b"), {}
        )
    except IntegrityError:
        pytest.fail("synthesized external_message_id for failed sends must be unique")


async def test_send_reply_enforces_whatsapp_rate_limit(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_id = await _make_conversation("org-a", ChannelType.WHATSAPP, "15550001111")
    monkeypatch.setitem(config_module.RATE_LIMITS, "omnichannel.whatsapp.send", (1, 3600))

    fake_client = Mock()
    fake_client.post = AsyncMock(return_value=_FakeWhatsAppResponse())
    monkeypatch.setattr(clients, "http_client", lambda: fake_client)
    credentials: dict[str, Any] = {"access_token": "tok", "phone_number_id": "pn1"}

    await handlers.send_reply(
        "org-a", conversation_id, "user-1", OutboundContent(body_text="hi"), credentials
    )

    with pytest.raises(RateLimitError):
        await handlers.send_reply(
            "org-a", conversation_id, "user-1", OutboundContent(body_text="hi again"), credentials
        )
