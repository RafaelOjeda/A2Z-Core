"""Worker: SQS consumer for Omni-Channel's inbound/outbound message flow (§5.6).

Runs as its own process at MVP (§12: same image, ``worker`` entrypoint, a
long-running loop calling these functions). Each ``process_*_batch`` drains
up to ``max_messages`` and returns the count it consumed from the queue, so
tests call it directly without a long-running process.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secrets
from app.core.events import publish_event
from app.core.logging import get_logger
from app.core.realtime import publish_update
from app.core.storage import upload_file
from app.services.omnichannel import queues
from app.services.omnichannel.adapters.registry import get_adapter
from app.services.omnichannel.adapters.types import OutboundContent
from app.services.omnichannel.exceptions import ChannelAdapterError
from app.services.omnichannel.models import (
    ChannelConnection,
    ChannelIdentity,
    Conversation,
    Message,
    MessageAttachment,
)

log = get_logger("omnichannel.worker")

# Bounded retry, then let it fall to the DLQ (§5.6) -- never blind infinite
# retry (WhatsApp sends cost real money per attempt). Alarming on DLQ depth
# is infra, Step 8.
_OUTBOUND_MAX_ATTEMPTS = 5


async def process_inbound_batch(session: AsyncSession, *, max_messages: int = 10) -> int:
    """Drain up to ``max_messages`` inbound webhook payloads.

    Returns:
        The number of SQS messages consumed (not the number of normalized
        customer messages -- one webhook call can batch several).
    """
    messages = await queues.receive_inbound(max_messages=max_messages)
    for msg in messages:
        await _process_inbound_message(session, msg)
        await queues.delete_inbound(msg.receipt_handle)
    return len(messages)


async def _process_inbound_message(session: AsyncSession, msg: queues.QueueMessage) -> None:
    org_id = msg.attributes["org_id"]
    channel_type = msg.attributes["channel_type"]
    adapter = get_adapter(channel_type)
    normalized = await adapter.normalize_inbound(msg.body["raw_payload"])

    for item in normalized:
        identity = await _find_or_create_identity(
            session, org_id, channel_type, item.external_id, item.display_name
        )
        conversation = await _find_or_create_conversation(session, org_id, identity.id)

        message = Message(
            org_id=org_id,
            conversation_id=conversation.id,
            direction="inbound",
            channel_type=channel_type,
            external_message_id=item.external_message_id,
            body_text=item.body_text,
            content_type=item.content_type,
            status="received",
        )
        session.add(message)
        try:
            # A savepoint, not the whole transaction: a duplicate delivery
            # (webhook retry) must not roll back the identity/conversation
            # this loop iteration may have just created.
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            # (channel_type, external_message_id) already exists -- Meta
            # retries webhooks aggressively; this is the idempotency
            # guarantee from models.py::uq_message_idempotency, not an error.
            # The failed flush leaves the ORM Session itself (not just the
            # DB-level savepoint) marked unusable until an explicit
            # rollback() -- begin_nested()'s own rollback-on-exception only
            # covers the SAVEPOINT, not the Session's unit-of-work state.
            await session.rollback()
            log.info(
                "omnichannel.inbound.duplicate",
                extra={"org_id": org_id, "external_message_id": item.external_message_id},
            )
            continue

        for att in item.attachments:
            stored = await upload_file(
                org_id, "omnichannel", att.filename, att.content, att.content_type, "system"
            )
            session.add(
                MessageAttachment(
                    message_id=message.id,
                    org_id=org_id,
                    s3_key=stored.key,
                    content_type=att.content_type,
                    size_bytes=len(att.content),
                )
            )

        conversation.last_message_at = datetime.now(timezone.utc)
        conversation.last_message_preview = (item.body_text or "")[:200]
        conversation.unread_count += 1
        await session.commit()

        # Routing (single-assignee / round-robin, §5.3) is Step 6 -- v1's
        # message flow leaves new conversations unassigned here, so there's
        # no assignee to notify yet either.
        await publish_event(
            org_id,
            "message.received",
            {
                "conversation_id": conversation.id,
                "message_id": message.id,
                "channel_type": channel_type,
            },
            source="a2z.omnichannel",
        )
        await publish_update(
            org_id,
            f"org:{org_id}:conversations",
            {
                "type": "message.received",
                "conversation_id": conversation.id,
                "message_id": message.id,
            },
        )
        log.info(
            "omnichannel.inbound.processed",
            extra={"org_id": org_id, "conversation_id": conversation.id, "message_id": message.id},
        )


async def _find_or_create_identity(
    session: AsyncSession,
    org_id: str,
    channel_type: str,
    external_id: str,
    display_name: str | None,
) -> ChannelIdentity:
    result = await session.execute(
        select(ChannelIdentity).where(
            ChannelIdentity.org_id == org_id,
            ChannelIdentity.channel_type == channel_type,
            ChannelIdentity.external_id == external_id,
        )
    )
    identity = result.scalar_one_or_none()
    if identity is not None:
        return identity
    identity = ChannelIdentity(
        org_id=org_id,
        channel_type=channel_type,
        external_id=external_id,
        display_name=display_name,
    )
    session.add(identity)
    await session.flush()
    return identity


async def _find_or_create_conversation(
    session: AsyncSession, org_id: str, customer_identity_id: str
) -> Conversation:
    # v1: one conversation per channel identity. Cross-channel merge (the
    # same customer's phone + email collapsing into one conversation) needs
    # `channel_identities.customer_id`, which is only ever set by an
    # agent-confirmed merge (docs/omnichannel-decisions.md, decision #2) --
    # not built yet, so there's no cross-identity lookup here.
    result = await session.execute(
        select(Conversation).where(
            Conversation.org_id == org_id,
            Conversation.customer_identity_id == customer_identity_id,
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation is not None:
        return conversation
    conversation = Conversation(
        org_id=org_id,
        customer_identity_id=customer_identity_id,
        status="open",
        unread_count=0,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def process_outbound_batch(session: AsyncSession, *, max_messages: int = 10) -> int:
    """Drain up to ``max_messages`` queued outbound sends.

    Returns:
        The number of SQS messages resolved (sent, or dropped as
        unrecoverable/exhausted) -- not just successful sends.
    """
    messages = await queues.receive_outbound(max_messages=max_messages)
    processed = 0
    for msg in messages:
        sent = await _process_outbound_message(session, msg)
        if sent or msg.receive_count >= _OUTBOUND_MAX_ATTEMPTS:
            await queues.delete_outbound(msg.receipt_handle)
            processed += 1
        # else: leave it in the queue -- SQS's visibility timeout drives the
        # retry/backoff; once maxReceiveCount is hit, the redrive policy
        # (scripts/create_local_resources.py; real infra at distribution)
        # moves it to the DLQ automatically.
    return processed


async def _process_outbound_message(session: AsyncSession, msg: queues.QueueMessage) -> bool:
    """Send one queued outbound message. Returns True iff it's safe to delete."""
    message_id = msg.body["message_id"]
    message = await session.get(Message, message_id)
    if message is None:
        log.error("omnichannel.outbound.missing_message", extra={"message_id": message_id})
        return True  # Nothing to retry.

    conversation = await session.get(Conversation, message.conversation_id)
    identity = (
        await session.get(ChannelIdentity, conversation.customer_identity_id)
        if conversation is not None
        else None
    )
    connection = await _find_connection(session, message.org_id, message.channel_type)
    if conversation is None or identity is None or connection is None:
        log.error("omnichannel.outbound.missing_context", extra={"message_id": message_id})
        return True

    adapter = get_adapter(message.channel_type)
    credentials: dict[str, Any] = {"org_id": message.org_id}
    if message.channel_type != "email":
        secret_bundle = await secrets.get_secret(
            message.org_id, "omnichannel", connection.credentials_secret_key
        )
        credentials.update(secret_bundle)

    try:
        result = await adapter.send_outbound(
            identity.external_id, OutboundContent(body_text=message.body_text), credentials
        )
    except ChannelAdapterError:
        log.error(
            "omnichannel.outbound.send_failed",
            extra={
                "org_id": message.org_id,
                "message_id": message_id,
                "attempt": msg.receive_count,
            },
        )
        if msg.receive_count >= _OUTBOUND_MAX_ATTEMPTS:
            message.status = "failed"
            await session.commit()
        return False

    message.external_message_id = result.external_message_id
    message.status = "sent"
    await session.commit()

    await publish_event(
        message.org_id,
        "message.sent",
        {"conversation_id": message.conversation_id, "message_id": message.id},
        source="a2z.omnichannel",
    )
    await publish_update(
        message.org_id,
        f"org:{message.org_id}:conversations",
        {
            "type": "message.sent",
            "conversation_id": message.conversation_id,
            "message_id": message.id,
        },
    )
    return True


async def _find_connection(
    session: AsyncSession, org_id: str, channel_type: str
) -> ChannelConnection | None:
    result = await session.execute(
        select(ChannelConnection).where(
            ChannelConnection.org_id == org_id, ChannelConnection.channel_type == channel_type
        )
    )
    return result.scalars().first()
