"""Shared access checks for Invoicing (§4).

Unlike Omni-Channel, there is no role-vocabulary gap here -- §4's permission
grid already uses Core's own OWNER/ADMIN/MEMBER/GUEST directly, so this is a
thin, direct wrapper around ``core.membership`` rather than a vocabulary
bridge (contrast with ``app/services/omnichannel/access.py``).
"""

from __future__ import annotations

from app.core.exceptions import NotFoundError
from app.core.membership import Membership, Role, get_membership
from app.services.invoicing.exceptions import InvoiceForbiddenError

# Create/edit/delete/send/void/record-payment: OWNER/ADMIN only (§4). Read
# (list/view invoices, view payments) is open to any member -- checked via
# require_membership alone, with no role filter.
MUTATION_ROLES: tuple[Role, ...] = (Role.OWNER, Role.ADMIN)

_NOT_A_MEMBER = "Not a member of this org"


async def require_membership(user_id: str, org_id: str) -> Membership:
    """Return the caller's membership, or raise ``NotFoundError`` (404) if
    they aren't a member. Satisfies every read-tier check (§4 grants every
    role read access)."""
    membership = await get_membership(user_id, org_id)
    if membership is None:
        raise NotFoundError(_NOT_A_MEMBER)
    return membership


async def require_mutation_role(user_id: str, org_id: str) -> Membership:
    """Require OWNER/ADMIN (§4). The 404 (not a member) is checked before the
    403 (member, wrong role) so a non-member can never distinguish
    "forbidden" from "no such org" -- mirrors Omni-Channel's ``require_role``."""
    membership = await require_membership(user_id, org_id)
    if membership.role not in MUTATION_ROLES:
        raise InvoiceForbiddenError("Requires owner or admin role")
    return membership
