"""Schema validation for the omnichannel Postgres tables (§5.1).

Exercises the baseline Alembic migration against a real Postgres: the
FK relationships, the org-scoped unique constraints, and the single most
load-bearing constraint in the whole service -- the
``(channel_type, external_message_id)`` uniqueness guarantee that makes
webhook retries safe to dedupe against (root CLAUDE.md golden rule #2 +
app/services/omnichannel/CLAUDE.md §5.1).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.services.omnichannel import db
from app.services.omnichannel.models import (
    ChannelIdentity,
    Conversation,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageStatus,
)


async def _make_identity_and_conversation(
    org_id: str, external_id: str = "+15550001111"
) -> tuple[ChannelIdentity, Conversation]:
    async with db.get_session() as session:
        identity = ChannelIdentity(org_id=org_id, channel_type="whatsapp", external_id=external_id)
        session.add(identity)
        await session.flush()

        conversation = Conversation(
            org_id=org_id, customer_identity_id=identity.id, status=ConversationStatus.OPEN
        )
        session.add(conversation)
        await session.flush()
        await session.refresh(identity)
        await session.refresh(conversation)
        return identity, conversation


async def test_insert_identity_and_conversation() -> None:
    identity, conversation = await _make_identity_and_conversation("org-a")
    assert conversation.customer_identity_id == identity.id
    assert conversation.status == ConversationStatus.OPEN
    assert conversation.unread_count == 0


async def test_message_unique_constraint_blocks_duplicate_webhook_delivery() -> None:
    _, conversation = await _make_identity_and_conversation("org-a")

    async with db.get_session() as session:
        session.add(
            Message(
                org_id="org-a",
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                channel_type="whatsapp",
                external_message_id="wamid.dup-1",
                status=MessageStatus.RECEIVED,
            )
        )

    # A retried webhook delivery with the same provider message id must be
    # rejected at the DB level -- this is what makes dedupe safe.
    with pytest.raises(IntegrityError):
        async with db.get_session() as session:
            session.add(
                Message(
                    org_id="org-a",
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND,
                    channel_type="whatsapp",
                    external_message_id="wamid.dup-1",
                    status=MessageStatus.RECEIVED,
                )
            )


async def test_channel_identity_unique_per_org_channel_and_external_id() -> None:
    await _make_identity_and_conversation("org-a", external_id="+15550002222")

    with pytest.raises(IntegrityError):
        async with db.get_session() as session:
            session.add(
                ChannelIdentity(org_id="org-a", channel_type="whatsapp", external_id="+15550002222")
            )


async def test_same_external_id_allowed_across_different_orgs() -> None:
    # Cross-org isolation: the same phone number for two different orgs is
    # not a collision -- the unique constraint is scoped by org_id.
    await _make_identity_and_conversation("org-a", external_id="+15550003333")
    await _make_identity_and_conversation("org-b", external_id="+15550003333")

    async with db.get_session() as session:
        rows = (
            (
                await session.execute(
                    select(ChannelIdentity).where(ChannelIdentity.external_id == "+15550003333")
                )
            )
            .scalars()
            .all()
        )
    assert {row.org_id for row in rows} == {"org-a", "org-b"}


async def test_message_fk_requires_existing_conversation() -> None:
    with pytest.raises(IntegrityError):
        async with db.get_session() as session:
            session.add(
                Message(
                    org_id="org-a",
                    conversation_id="00000000-0000-0000-0000-000000000000",
                    direction=MessageDirection.INBOUND,
                    channel_type="whatsapp",
                    external_message_id="wamid.orphan",
                    status=MessageStatus.RECEIVED,
                )
            )
