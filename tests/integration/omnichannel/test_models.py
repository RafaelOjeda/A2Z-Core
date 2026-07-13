"""Integration tests for Omni-Channel's Postgres schema (CLAUDE.md §5.1)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.omnichannel.models import ChannelIdentity, Conversation, Message

pytestmark = pytest.mark.integration


async def _make_identity(
    session: AsyncSession, org_id: str, external_id: str = "+15550001111"
) -> ChannelIdentity:
    identity = ChannelIdentity(org_id=org_id, channel_type="whatsapp", external_id=external_id)
    session.add(identity)
    await session.commit()
    return identity


async def _make_conversation(session: AsyncSession, org_id: str, identity_id: str) -> Conversation:
    convo = Conversation(org_id=org_id, customer_identity_id=identity_id)
    session.add(convo)
    await session.commit()
    return convo


async def test_message_idempotency_unique_constraint(session: AsyncSession) -> None:
    """(channel_type, external_message_id) is the webhook-idempotency guarantee (§5.1, §5.6)."""
    identity = await _make_identity(session, "org-a")
    convo = await _make_conversation(session, "org-a", identity.id)

    msg = Message(
        org_id="org-a",
        conversation_id=convo.id,
        direction="inbound",
        channel_type="whatsapp",
        external_message_id="wamid.abc123",
        body_text="hello",
    )
    session.add(msg)
    await session.commit()

    dup = Message(
        org_id="org-a",
        conversation_id=convo.id,
        direction="inbound",
        channel_type="whatsapp",
        external_message_id="wamid.abc123",  # same pair -- simulates a webhook retry
        body_text="retry of the same webhook delivery",
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_channel_identity_unique_constraint(session: AsyncSession) -> None:
    await _make_identity(session, "org-a", "+15550001111")

    dup = ChannelIdentity(org_id="org-a", channel_type="whatsapp", external_id="+15550001111")
    session.add(dup)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_conversation_requires_existing_identity(session: AsyncSession) -> None:
    orphan = Conversation(org_id="org-a", customer_identity_id="does-not-exist")
    session.add(orphan)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_cross_org_query_isolation(session: AsyncSession) -> None:
    identity_a = await _make_identity(session, "org-a", "+15550001111")
    identity_b = await _make_identity(session, "org-b", "+15550002222")
    await _make_conversation(session, "org-a", identity_a.id)
    await _make_conversation(session, "org-b", identity_b.id)

    result = await session.execute(select(Conversation).where(Conversation.org_id == "org-a"))
    rows = result.scalars().all()

    assert len(rows) == 1
    assert rows[0].org_id == "org-a"


async def test_channel_type_is_text_not_enum(session: AsyncSession) -> None:
    """Extensibility invariant (§5.1, §5.2): adding a channel must never need a migration."""
    result = await session.execute(
        text(
            "select data_type from information_schema.columns "
            "where table_schema = 'omnichannel' and table_name = 'messages' "
            "and column_name = 'channel_type'"
        )
    )
    assert result.scalar_one() == "text"
