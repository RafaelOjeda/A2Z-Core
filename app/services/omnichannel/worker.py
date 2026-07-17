"""Worker: SQS consumer for Omni-Channel's inbound/outbound message flow (§5.6).

Runs as its own process at MVP (§12: same image, ``worker`` entrypoint, a
long-running loop calling these functions). Each ``process_*_batch`` drains
up to ``max_messages`` and returns the count it consumed from the queue, so
tests call it directly without a long-running process.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SQS_MAX_RECEIVE_COUNT
from app.core import secrets
from app.core.events import publish_event
from app.core.logging import get_logger
from app.core.realtime import publish_update
from app.core.storage import upload_file
from app.services.omnichannel import metrics, queues, routing
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
# retry (WhatsApp sends cost real money per attempt). This is the *same*
# threshold as the queue's RedrivePolicy (config.SQS_MAX_RECEIVE_COUNT), read
# from one place on purpose: at this receive count we mark the message failed
# in Postgres so the UI reflects it, and SQS redrives the message itself to
# the DLQ. Two independent constants would drift and desync those two facts.
_OUTBOUND_MAX_ATTEMPTS = SQS_MAX_RECEIVE_COUNT


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
    started = time.perf_counter()
    org_id = msg.attributes["org_id"]
    channel_type = msg.attributes["channel_type"]
    adapter = get_adapter(channel_type)
    normalized = await adapter.normalize_inbound(msg.body["raw_payload"])

    for item in normalized:
        identity = await _find_or_create_identity(
            session, org_id, channel_type, item.external_id, item.display_name
        )
        conversation, conversation_is_new = await _find_or_create_conversation(
            session, org_id, identity.id
        )

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

        # Auto-routing only applies to a *brand-new* conversation -- an
        # existing one may already be claimed/reassigned, and single-assignee
        # must never override that (§5.3). Round-robin/sticky are deferred
        # (§15); apply_single_assignee_if_configured no-ops for any org that
        # hasn't opted into single-assignee via routing.set_routing_config.
        if conversation_is_new:
            await routing.apply_single_assignee_if_configured(session, conversation)

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
        if conversation.assigned_user_id:
            # Notify the assignee's own channel too (§5.6: "... -> notify
            # assignee"), on top of the org-wide inbox update above.
            await publish_update(
                org_id,
                f"user:{conversation.assigned_user_id}:notifications",
                {
                    "type": "message.received",
                    "conversation_id": conversation.id,
                    "message_id": message.id,
                },
            )
        # "Receipt -> visible in inbox" (§11): the realtime publish above is
        # the moment it lands on an agent's screen, so the span ends here.
        metrics.record_message_processing_latency(
            channel_type, (time.perf_counter() - started) * 1000
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
) -> tuple[Conversation, bool]:
    """Returns ``(conversation, created)`` -- ``created`` gates auto-routing (§5.3)."""
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
        return conversation, False
    conversation = Conversation(
        org_id=org_id,
        customer_identity_id=customer_identity_id,
        status="open",
        unread_count=0,
    )
    session.add(conversation)
    await session.flush()
    return conversation, True


async def process_outbound_batch(session: AsyncSession, *, max_messages: int = 10) -> int:
    """Drain up to ``max_messages`` queued outbound sends.

    Returns:
        The number of SQS messages deleted from the queue -- i.e. sent, or
        unrecoverable (no such message/context, so retrying can't help).
        A *failed send* is deliberately not counted or deleted; see below.
    """
    messages = await queues.receive_outbound(max_messages=max_messages)
    processed = 0
    for msg in messages:
        resolved = await _process_outbound_message(session, msg)
        if resolved:
            await queues.delete_outbound(msg.receipt_handle)
            processed += 1
        # else: leave it in the queue. SQS's visibility timeout drives the
        # retry/backoff, and once the message has been received more than
        # config.SQS_MAX_RECEIVE_COUNT times the redrive policy moves it to
        # the DLQ -- which is what the §11 "DLQ depth > 0" alarm watches.
        # Deleting an exhausted send here instead would retire it ourselves
        # and leave the DLQ permanently empty, silently disarming that alarm.
    return processed


async def _process_outbound_message(session: AsyncSession, msg: queues.QueueMessage) -> bool:
    """Send one queued outbound message.

    Returns:
        True iff the message is resolved and safe to delete from the queue --
        either sent, or unrecoverable so that retrying cannot help. False on a
        failed send, leaving SQS to retry and ultimately redrive it to the DLQ.
    """
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
        metrics.record_send_result(message.channel_type, success=False)
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
    metrics.record_send_result(message.channel_type, success=True)

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
