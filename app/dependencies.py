"""Shared FastAPI dependencies (current user / current membership).

Thin glue only — all logic lives in ``app.core``. These resolve identity and
org membership for the admin/testing routers (Design §7.2/§7.3). Role
interpretation stays with the caller: Core just returns the role string.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from app.core import auth, membership
from app.core.cache import register_clearable
from app.core.exceptions import CoreError, NotFoundError
from app.core.membership import Membership, Role

# User ids already confirmed provisioned this process's lifetime -- skips the
# (idempotent, but not free) DynamoDB conditional write on every request from
# an already-known user. Registered so tests reset it like any other cache
# (a moto-backed test wiping the membership table mid-suite must not leave
# this set claiming a user still exists).
_provisioned: set[str] = set()
register_clearable(_provisioned.clear)


async def current_user(request: Request) -> dict[str, Any]:
    """Validate the bearer token, return JWT claims, and provision the user row.

    ``membership.create_user_if_not_exists`` used to run out-of-band in the
    Cognito post-confirm Lambda (root CLAUDE.md §5). There is no Lambda on
    the single-box MVP (docs/architecture/single-box-mvp.md), and §5 already
    said first-login org bootstrap should prefer "the first authenticated
    request" over the Lambda -- this just extends that same reasoning one
    step further: bare user-row creation moves here too, so every
    authenticated request is also "the first one" as far as provisioning is
    concerned, at least until ``_provisioned`` remembers otherwise.
    """
    claims = auth.get_current_user_from_request(request)
    sub = claims.get("sub")
    email = claims.get("email")
    if sub and email and sub not in _provisioned:
        await membership.create_user_if_not_exists(sub, email)
        _provisioned.add(sub)
    return claims


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
