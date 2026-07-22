"""Routing -- v1 scope: manual claim/reassign + the single-assignee strategy (§5.3).

Round-robin and sticky are deferred (§15); this module covers exactly what
v1 ships: an agent claiming an unassigned conversation, an Owner/Admin
reassigning one, and the one auto-strategy built now -- single-assignee,
where every new conversation is pre-assigned to one designated user (solo
businesses; the owner *is* the inbox). Every assignment -- claim, reassign,
or single-assignee auto-apply -- writes an append-only
``ConversationAssignment`` row plus a ``core.audit`` entry and a realtime
update (§5.4: "assignment change" is one of the live-update triggers)
through the same internal helper, so the history and the live UI are
consistent regardless of which path produced it. That history is also what
makes commission (§5.5) replayable once Invoicing exists.

Role gates go through ``access.require_role`` / ``require_membership``, which
own the MEMBER -> Agent / GUEST -> Viewer mapping (root CLAUDE.md §14).
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_audit
from app.core.events import publish_event
from app.core.realtime import publish_update
from app.core.settings import get_org_settings, set_org_settings
from app.services.omnichannel import access, metrics
from app.services.omnichannel.exceptions import (
    ConversationAlreadyAssignedError,
    RoutingError,
)
from app.services.omnichannel.models import Conversation, ConversationAssignment

_METADATA_KEY = "omnichannel"
_SUPPORTED_STRATEGIES = ("manual", "single_assignee")


async def _record_assignment(
    session: AsyncSession,
    conversation: Conversation,
    assigned_user_id: str,
    assigned_by: str,
    reason: str,
) -> None:
    """Write the assignment + history row + audit + domain event + realtime update.

    Shared by every assignment path (claim, reassign, single-assignee
    auto-apply) so the history is uniform no matter who/what triggered it.
    """
    started = time.perf_counter()
    conversation.assigned_user_id = assigned_user_id
    session.add(
        ConversationAssignment(
            org_id=conversation.org_id,
            conversation_id=conversation.id,
            assigned_user_id=assigned_user_id,
            assigned_by=assigned_by,
            reason=reason,
        )
    )
    await session.commit()
    await log_audit(
        conversation.org_id,
        assigned_by,
        "conversation.assigned",
        "conversation",
        conversation.id,
        {"assigned_user_id": assigned_user_id, "reason": reason},
    )
    # The cross-service contract (§6.1 lists conversation.assigned among the
    # events this service publishes). Distinct from the realtime update below:
    # this is EventBridge, for other services; that one is the UI push.
    await publish_event(
        conversation.org_id,
        "conversation.assigned",
        {
            "conversation_id": conversation.id,
            "assigned_user_id": assigned_user_id,
            "assigned_by": assigned_by,
            "reason": reason,
        },
        source="a2z.omnichannel",
    )
    await publish_update(
        conversation.org_id,
        f"org:{conversation.org_id}:conversations",
        {
            "type": "conversation.assigned",
            "conversation_id": conversation.id,
            "assigned_user_id": assigned_user_id,
        },
    )
    await publish_update(
        conversation.org_id,
        f"user:{assigned_user_id}:notifications",
        {
            "type": "conversation.assigned",
            "conversation_id": conversation.id,
            "assigned_user_id": assigned_user_id,
        },
    )
    metrics.record_routing_latency((time.perf_counter() - started) * 1000)


async def claim(
    session: AsyncSession, org_id: str, conversation_id: str, user_id: str
) -> Conversation:
    """An agent claims an unassigned conversation (§4: Owner/Admin/Agent, not Viewer).

    Idempotent if the caller already owns it -- returns as-is, no new
    history row. Raises if it's assigned to someone else: that's a
    reassign, a different (more restricted) action.

    Raises:
        NotFoundError: Caller isn't a member of ``org_id``.
        ForbiddenError: Caller's role can't claim (Viewer-equivalent).
        ConversationNotFoundError: No such conversation for this org.
        ConversationAlreadyAssignedError: Already assigned to someone else.
    """
    await access.require_role(
        user_id,
        org_id,
        access.NON_VIEWER_ROLES,
        forbidden_message="Viewers cannot claim conversations",
    )

    conversation = await access.load_conversation(session, org_id, conversation_id)
    if conversation.assigned_user_id == user_id:
        return conversation
    if conversation.assigned_user_id is not None:
        raise ConversationAlreadyAssignedError(
            f"Conversation {conversation_id!r} is already assigned; use reassign"
        )

    await _record_assignment(session, conversation, user_id, user_id, "claim")
    return conversation


async def reassign(
    session: AsyncSession,
    org_id: str,
    conversation_id: str,
    actor_user_id: str,
    assignee_user_id: str,
) -> Conversation:
    """Owner/Admin reassigns a conversation to a different member (§4).

    Raises:
        NotFoundError: ``actor_user_id`` or ``assignee_user_id`` isn't a
            member of ``org_id``.
        ForbiddenError: Actor's role can't reassign (only Owner/Admin can).
        ConversationNotFoundError: No such conversation for this org.
    """
    await access.require_role(
        actor_user_id,
        org_id,
        access.ADMIN_ROLES,
        forbidden_message="Only Owner/Admin can reassign a conversation",
    )
    await access.require_membership(
        assignee_user_id,
        org_id,
        message=f"{assignee_user_id!r} is not a member of this org",
    )

    conversation = await access.load_conversation(session, org_id, conversation_id)
    await _record_assignment(session, conversation, assignee_user_id, actor_user_id, "reassign")
    return conversation


async def apply_single_assignee_if_configured(
    session: AsyncSession, conversation: Conversation
) -> None:
    """Auto-assign a brand-new conversation under the single-assignee strategy (§5.3).

    Called from the inbound worker right after a *new* conversation is
    created (never for an existing one -- claim/reassign own that). No-ops,
    leaving the conversation unassigned in the shared inbox, unless the org
    has explicitly configured single-assignee routing via
    ``set_routing_config``. Round-robin/sticky are deferred (§15) -- there's
    no branch for them here.
    """
    org_settings = await get_org_settings(conversation.org_id)
    config = org_settings.metadata.get(_METADATA_KEY, {})
    if config.get("routing_strategy") != "single_assignee":
        return
    designated_user_id = config.get("single_assignee_user_id")
    if not designated_user_id:
        return
    await _record_assignment(
        session, conversation, designated_user_id, "routing:single_assignee", "single_assignee"
    )


async def set_routing_config(
    org_id: str,
    actor_user_id: str,
    strategy: str,
    single_assignee_user_id: str | None = None,
) -> dict[str, Any]:
    """Set the org's routing strategy (§4: Owner/Admin only; §5.3 for the shape).

    Stored in ``core.settings``' free-form ``metadata`` field, namespaced
    under ``"omnichannel"`` -- Core's settings schema is fixed (Design §2.6)
    and this is exactly the escape hatch it provides for service-specific
    config, so no Core change is needed.

    v1 only implements ``"manual"`` (the default -- no auto-strategy) and
    ``"single_assignee"``; round-robin/sticky are deferred (§15) and
    rejected here rather than silently accepted and ignored.

    Raises:
        NotFoundError: Actor (or the designated single-assignee user) isn't
            a member of ``org_id``.
        ForbiddenError: Actor's role can't configure routing.
        RoutingError: Unknown/unsupported strategy, or ``single_assignee``
            without a designated user.
    """
    await access.require_role(
        actor_user_id,
        org_id,
        access.ADMIN_ROLES,
        forbidden_message="Only Owner/Admin can configure routing",
    )

    if strategy not in _SUPPORTED_STRATEGIES:
        raise RoutingError(
            f"Unsupported routing strategy {strategy!r} (round-robin/sticky are deferred, §15)"
        )
    if strategy == "single_assignee":
        if not single_assignee_user_id:
            raise RoutingError("single_assignee requires single_assignee_user_id")
        await access.require_membership(
            single_assignee_user_id,
            org_id,
            message=f"{single_assignee_user_id!r} is not a member of this org",
        )

    org_settings = await get_org_settings(org_id)
    metadata = dict(org_settings.metadata)
    metadata[_METADATA_KEY] = {
        "routing_strategy": strategy,
        "single_assignee_user_id": single_assignee_user_id,
    }
    await set_org_settings(org_id, {"metadata": metadata}, actor_user_id)
    result: dict[str, Any] = metadata[_METADATA_KEY]
    return result
