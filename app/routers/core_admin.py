"""Core admin router — thin HTTP over Core for admin/testing (CLAUDE.md §2).

Routers parse the request, call ``core``, and return the result. No business
logic lives here. Role checks are hardcoded via dependencies (no RBAC service).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.core import email, membership, settings
from app.core.email import EmailResult, ServiceType
from app.core.membership import Membership, Org, Role
from app.core.settings import OrgSettings
from app.dependencies import CurrentUser, require_admin, require_member

router = APIRouter(prefix="/core", tags=["core-admin"])


# --- Request bodies ---
class CreateOrgRequest(BaseModel):
    name: str


class AddMemberRequest(BaseModel):
    user_id: str
    role: Role = Role.MEMBER


class UpdateSettingsRequest(BaseModel):
    changes: dict[str, Any]


class SendEmailRequest(BaseModel):
    org_id: str
    service_type: ServiceType
    to: str
    subject: str
    body_html: str
    body_text: str | None = None
    metadata: dict[str, Any] | None = None


# --- Endpoints ---
@router.post("/orgs", status_code=201)
async def create_org(body: CreateOrgRequest, user: CurrentUser) -> Org:
    return await membership.create_org(body.name, user["sub"])


@router.get("/orgs/{org_id}/members")
async def list_members(org_id: str, user: CurrentUser) -> list[Membership]:
    await require_member(org_id, user)
    return await membership.list_org_members(org_id)


@router.post("/orgs/{org_id}/members", status_code=201)
async def add_member(org_id: str, body: AddMemberRequest, user: CurrentUser) -> Membership:
    m = await require_member(org_id, user)
    require_admin(m)
    return await membership.add_member(org_id, body.user_id, body.role, user["sub"])


@router.get("/orgs/{org_id}/settings")
async def get_settings(org_id: str, user: CurrentUser) -> OrgSettings:
    await require_member(org_id, user)
    return await settings.get_org_settings(org_id)


@router.patch("/orgs/{org_id}/settings")
async def update_settings(
    org_id: str, body: UpdateSettingsRequest, user: CurrentUser
) -> OrgSettings:
    m = await require_member(org_id, user)
    require_admin(m)
    return await settings.set_org_settings(org_id, body.changes, user["sub"])


@router.post("/email/send")
async def send_email(body: SendEmailRequest, user: CurrentUser) -> EmailResult:
    await require_member(body.org_id, user)
    return await email.send_email(
        org_id=body.org_id,
        service_type=body.service_type,
        to=body.to,
        subject=body.subject,
        body_html=body.body_html,
        body_text=body.body_text,
        metadata=body.metadata,
    )


@router.get("/orgs/{org_id}/domain-verification")
async def get_domain_verification_status(org_id: str, user: CurrentUser) -> dict[str, str]:
    """Poll SES's live verification status for the org's configured domain.

    Lets a "connect your channel" UI show "waiting on DNS..." vs "verified"
    without anyone checking the SES console by hand.
    """
    await require_member(org_id, user)
    status = await email.get_domain_verification_status(org_id)
    return {"status": status}
