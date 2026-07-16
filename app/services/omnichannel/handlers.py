"""Business logic called by routers (root CLAUDE.md §2: routers stay thin).

Currently just the outbound half of the message flow (§5.6): authz, rate
limiting, persistence as ``queued``, and enqueueing to the worker. The
inbound half lives in ``webhooks.py`` (verify + enqueue) and ``worker.py``
(both halves' actual processing).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import RATE_LIMITS
from app.core import rate_limit
from app.core.exceptions import NotFoundError
from app.core.membership import Role, get_membership
from app.services.omnichannel import queues
from app.services.omnichannel.exceptions import ConversationNotFoundError, OmniChannelError
from app.services.omnichannel.models import ChannelIdentity, Conversation, Message


class ForbiddenError(OmniChannelError):
    """Caller's role can't perform this action (§4)."""

    status_code = 403


def _rate_limit_action(channel_type: str) -> str:
    return f"omnichannel.{channel_type}.send"


async def send_reply(
    session: AsyncSession,
    org_id: str,
    conversation_id: str,
    user_id: str,
    body_text: str,
) -> Message:
    """Send an agent's reply in a conversation (§5.6, the outbound half).

    Persists the ``Message`` as ``queued`` and enqueues it for the worker;
    the actual channel send (and marking it ``sent``/``failed``) happens
    asynchronously in ``worker.process_outbound_batch``.

    Note on roles: §4's table uses "Agent"/"Viewer", but ``core.membership``
    only defines OWNER/ADMIN/MEMBER/GUEST (root CLAUDE.md §14 -- there is no
    Permissions service, and interpreting the role string is this service's
    job). This maps MEMBER -> Agent and GUEST -> Viewer, the closest fit to
    §4's permission grid: everyone except GUEST/Viewer can reply.

    Raises:
        NotFoundError: Caller isn't a member of ``org_id``.
        ForbiddenError: Caller's role can't send (Viewer-equivalent, §4).
        ConversationNotFoundError: No such conversation for this org.
        RateLimitError: The channel's outbound rate limit was exceeded.

    Performance target: < 200ms for this handler (persist + enqueue only --
    the actual channel send happens in the worker, §5.6).
    """
    membership = await get_membership(user_id, org_id)
    if membership is None:
        raise NotFoundError("Not a member of this org")
    if membership.role == Role.GUEST:
        raise ForbiddenError("Viewers cannot send outbound messages")

    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.org_id != org_id:
        raise ConversationNotFoundError(f"No conversation {conversation_id!r} for org {org_id!r}")

    identity = await session.get(ChannelIdentity, conversation.customer_identity_id)
    if identity is None:
        raise ConversationNotFoundError(
            f"Conversation {conversation_id!r} has no customer identity"
        )
    channel_type = identity.channel_type

    action = _rate_limit_action(channel_type)
    limits = RATE_LIMITS.get(action)
    if limits is not None:
        # Not every channel has its own entry -- email's outbound rate limit
        # is already enforced inside core.email.send_email (§6.4), so there's
        # deliberately no "omnichannel.email.send" key to look up here.
        limit, window = limits
        await rate_limit.check_and_increment(org_id, action, limit=limit, window_seconds=window)

    message = Message(
        org_id=org_id,
        conversation_id=conversation_id,
        direction="outbound",
        channel_type=channel_type,
        # Placeholder, unique and non-null to satisfy uq_message_idempotency
        # until the worker overwrites it with the provider's real id after
        # send (models.py: external_message_id is NOT NULL + unique).
        external_message_id=f"pending:{uuid.uuid4()}",
        body_text=body_text,
        content_type="text/plain",
        status="queued",
        sent_by_user_id=user_id,
    )
    session.add(message)
    await session.commit()

    await queues.enqueue_outbound(org_id=org_id, message_id=message.id)
    return message
