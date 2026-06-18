"""Shared FastAPI dependencies (current user / current membership).

Thin glue only — all logic lives in ``app.core``. These resolve identity and
org membership for the admin/testing routers (Design §7.2/§7.3). Role
interpretation stays with the caller: Core just returns the role string.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from app.core import auth, membership
from app.core.exceptions import CoreError, NotFoundError
from app.core.membership import Membership, Role


def current_user(request: Request) -> dict[str, Any]:
    """Validate the bearer token and return JWT claims."""
    return auth.get_current_user_from_request(request)


CurrentUser = Annotated[dict[str, Any], Depends(current_user)]


async def require_member(org_id: str, user: CurrentUser) -> Membership:
    """Ensure the caller belongs to the org; return their membership.

    Raises NotFoundError (404) if the user is not a member — routers map this to
    an HTTP response via the CoreError handler.
    """
    m = await membership.get_membership(user["sub"], org_id)
    if m is None:
        raise NotFoundError("Not a member of this org")
    return m


def require_admin(m: Membership) -> None:
    """Hardcoded role gate (no RBAC service yet — CLAUDE.md §14)."""
    if m.role not in (Role.OWNER, Role.ADMIN):
        raise _Forbidden("Requires owner or admin role")


class _Forbidden(CoreError):
    status_code = 403
