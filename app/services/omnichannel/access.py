"""Shared access checks: membership/role gates and org-scoped conversation loads.

Every Omni-Channel handler opens with the same guards before doing any work:
resolve the caller's membership (404 if they aren't in the org) and, for write
actions, check their role (403 if it's too low). And every path that touches a
single conversation first loads it *scoped to the org*, so that another org's
conversation id is indistinguishable from one that doesn't exist. Both were
hand-inlined in ``handlers``, ``routing``, ``connections``, ``inbox`` and the
router; centralizing them here keeps the role->action mapping documented once
instead of re-explained in every module.

Role mapping (root CLAUDE.md §14 -- there is no Permissions service; each
service interprets the role string itself). §4's grid uses
Owner/Admin/Agent/Viewer, but ``core.membership.Role`` only defines
OWNER/ADMIN/MEMBER/GUEST, so this service maps **MEMBER -> Agent** and
**GUEST -> Viewer** everywhere.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.membership import Membership, Role, get_membership
from app.services.omnichannel.exceptions import ConversationNotFoundError, ForbiddenError
from app.services.omnichannel.models import Conversation

# Everyone except a Viewer (GUEST) may send/claim -- the write-but-not-admin tier.
NON_VIEWER_ROLES: tuple[Role, ...] = (Role.OWNER, Role.ADMIN, Role.MEMBER)
# Owner/Admin only -- channel config, reassignment, routing config.
ADMIN_ROLES: tuple[Role, ...] = (Role.OWNER, Role.ADMIN)

_NOT_A_MEMBER = "Not a member of this org"


async def require_membership(
    user_id: str, org_id: str, *, message: str = _NOT_A_MEMBER
) -> Membership:
    """Return the caller's membership, or raise ``NotFoundError`` (404) if none.

    Any role satisfies this: it is the read tier (§4 grants every role read
    access). Write paths layer :func:`require_role` on top. ``message`` is
    parameterized so callers checking a *different* user's membership (an
    assignment target, say) can name that user in the error.
    """
    membership = await get_membership(user_id, org_id)
    if membership is None:
        raise NotFoundError(message)
    return membership


async def require_role(
    user_id: str, org_id: str, allowed: tuple[Role, ...], *, forbidden_message: str
) -> Membership:
    """Require membership *and* one of ``allowed`` roles, else 404 / 403.

    The 404 (not a member) is checked before the 403 (member, wrong role) so a
    non-member can never distinguish "forbidden" from "no such org".
    """
    membership = await require_membership(user_id, org_id)
    if membership.role not in allowed:
        raise ForbiddenError(forbidden_message)
    return membership


async def load_conversation(
    session: AsyncSession, org_id: str, conversation_id: str
) -> Conversation:
    """Load a conversation scoped to ``org_id``, or raise ``ConversationNotFoundError``.

    Same error whether the row doesn't exist or belongs to another org --
    cross-org existence is itself information we don't hand out.
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.org_id != org_id:
        raise ConversationNotFoundError(f"No conversation {conversation_id!r} for org {org_id!r}")
    return conversation
