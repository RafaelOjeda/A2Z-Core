"""Membership — User -> Org -> Role tenancy model (Design §2.2).

Single DynamoDB table (``a2z-core-membership``) using the adjacency-list pattern:

  * ``USER#{sub} / METADATA``        — user record (email, created_at)
  * ``ORG#{org_id} / METADATA``      — org record (name, owner_id, created_at)
  * ``USER#{sub} / ORG#{org_id}``    — membership; GSI1 inverts it to
                                        ``ORG#{org_id} / USER#{sub}`` so we can
                                        list an org's members in one query.

Every read is org-scoped. Mutations log to audit. Cross-service ``member.*``
events are published once the events module exists (CLAUDE.md build order §13);
wired in at that step.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from enum import Enum

from botocore.exceptions import ClientError
from pydantic import BaseModel

from app.config import settings
from app.core import clients
from app.core._ddb import from_item, to_item
from app.core.audit import ActionType, log_audit
from app.core.exceptions import AlreadyExistsError, MembershipError, NotFoundError
from app.core.logging import get_logger

log = get_logger("core.membership")


class Role(str, Enum):
    """User roles in an org."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    GUEST = "guest"


# For sorting members owner-first.
_ROLE_ORDER = {Role.OWNER: 0, Role.ADMIN: 1, Role.MEMBER: 2, Role.GUEST: 3}


class Membership(BaseModel):
    user_id: str
    org_id: str
    role: Role
    joined_at: datetime


class Org(BaseModel):
    org_id: str
    name: str
    owner_id: str
    created_at: datetime


def _table() -> str:
    return settings().tables["membership"]


def _user_pk(user_id: str) -> str:
    return f"USER#{user_id}"


def _org_pk(org_id: str) -> str:
    return f"ORG#{org_id}"


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return base or "org"


# ===== Queries =====


async def get_membership(user_id: str, org_id: str) -> Membership | None:
    """Return the user's membership in an org, or ``None`` (Design §2.2).

    Performance: < 50ms (single DynamoDB get).
    """
    resp = await clients.run_aws(
        clients.dynamodb().get_item,
        TableName=_table(),
        Key=to_item({"PK": _user_pk(user_id), "SK": _org_pk(org_id)}),
    )
    item = resp.get("Item")
    if not item:
        return None
    data = from_item(item)
    return Membership(
        user_id=user_id,
        org_id=org_id,
        role=Role(data["role"]),
        joined_at=datetime.fromisoformat(data["joined_at"]),
    )


async def list_user_orgs(user_id: str) -> list[Org]:
    """List all orgs a user belongs to (Design §2.2). Performance: < 100ms."""
    resp = await clients.run_aws(
        clients.dynamodb().query,
        TableName=_table(),
        KeyConditionExpression="PK = :pk AND begins_with(SK, :org)",
        ExpressionAttributeValues=to_item({":pk": _user_pk(user_id), ":org": "ORG#"}),
    )
    org_ids = [from_item(it)["SK"].split("ORG#", 1)[1] for it in resp.get("Items", [])]
    orgs = await asyncio.gather(*(_get_org(o) for o in org_ids))
    return [o for o in orgs if o is not None]


async def _get_org(org_id: str) -> Org | None:
    resp = await clients.run_aws(
        clients.dynamodb().get_item,
        TableName=_table(),
        Key=to_item({"PK": _org_pk(org_id), "SK": "METADATA"}),
    )
    item = resp.get("Item")
    if not item:
        return None
    data = from_item(item)
    return Org(
        org_id=org_id,
        name=data["name"],
        owner_id=data["owner_id"],
        created_at=datetime.fromisoformat(data["created_at"]),
    )


async def list_org_members(org_id: str) -> list[Membership]:
    """List all members of an org via GSI1, owner-first (Design §2.2).

    Performance: < 200ms.
    """
    resp = await clients.run_aws(
        clients.dynamodb().query,
        TableName=_table(),
        IndexName="GSI1",
        KeyConditionExpression="GSI1PK = :org",
        ExpressionAttributeValues=to_item({":org": _org_pk(org_id)}),
    )
    members = [
        Membership(
            user_id=d["GSI1SK"].split("USER#", 1)[1],
            org_id=org_id,
            role=Role(d["role"]),
            joined_at=datetime.fromisoformat(d["joined_at"]),
        )
        for d in (from_item(it) for it in resp.get("Items", []))
    ]
    members.sort(key=lambda m: _ROLE_ORDER.get(m.role, 99))
    return members


# ===== Mutations =====


async def create_org(org_name: str, owner_id: str) -> Org:
    """Create an org and add the creator as OWNER (Design §2.2).

    Atomic (TransactWriteItems): writes the org record and the owner membership
    together. Logs ``org.created``. Performance: < 100ms.
    """
    now = datetime.now(timezone.utc)
    org_id = f"{_slug(org_name)}-{uuid.uuid4().hex[:8]}"
    org = Org(org_id=org_id, name=org_name, owner_id=owner_id, created_at=now)

    org_item = to_item(
        {"PK": _org_pk(org_id), "SK": "METADATA", "name": org_name,
         "owner_id": owner_id, "created_at": now.isoformat()}
    )
    member_item = to_item(_membership_item(org_id, owner_id, Role.OWNER, now))
    try:
        await clients.run_aws(
            clients.dynamodb().transact_write_items,
            TransactItems=[
                {"Put": {"TableName": _table(), "Item": org_item}},
                {"Put": {"TableName": _table(), "Item": member_item}},
            ],
        )
    except ClientError as exc:
        raise MembershipError(f"Failed to create org: {exc}") from exc

    await log_audit(org_id, owner_id, ActionType.ORG_CREATED, "org", org_id,
                    {"name": org_name})
    log.info("org.created", extra={"org_id": org_id, "owner_id": owner_id})
    return org


async def add_member(
    org_id: str, user_id: str, role: Role, inviter_id: str
) -> Membership:
    """Add a user to an org with a role (Design §2.2).

    Raises AlreadyExistsError if the user is already in the org. Logs
    ``member.added``. Performance: < 100ms.
    """
    now = datetime.now(timezone.utc)
    try:
        await clients.run_aws(
            clients.dynamodb().put_item,
            TableName=_table(),
            Item=to_item(_membership_item(org_id, user_id, role, now)),
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise AlreadyExistsError(
                f"User {user_id} already in org {org_id}"
            ) from exc
        raise MembershipError(f"Failed to add member: {exc}") from exc

    await log_audit(org_id, inviter_id, ActionType.MEMBER_ADDED, "user", user_id,
                    {"role": role.value})
    log.info("member.added", extra={"org_id": org_id, "user_id": user_id})
    return Membership(user_id=user_id, org_id=org_id, role=role, joined_at=now)


async def change_role(
    org_id: str, user_id: str, new_role: Role, changer_id: str
) -> Membership:
    """Change a member's role (Design §2.2).

    Raises NotFoundError if the membership doesn't exist. Logs
    ``member.role_changed`` with old/new role. Performance: < 100ms.
    """
    try:
        resp = await clients.run_aws(
            clients.dynamodb().update_item,
            TableName=_table(),
            Key=to_item({"PK": _user_pk(user_id), "SK": _org_pk(org_id)}),
            UpdateExpression="SET #role = :new",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames={"#role": "role"},
            ExpressionAttributeValues=to_item({":new": new_role.value}),
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise NotFoundError(
                f"No membership for user {user_id} in org {org_id}"
            ) from exc
        raise MembershipError(f"Failed to change role: {exc}") from exc

    old = from_item(resp.get("Attributes", {})).get("role")
    await log_audit(
        org_id, changer_id, ActionType.MEMBER_ROLE_CHANGED, "user", user_id,
        {"old_role": old, "new_role": new_role.value},
    )
    log.info("member.role_changed", extra={"org_id": org_id, "user_id": user_id})
    joined = from_item(resp.get("Attributes", {})).get("joined_at")
    return Membership(
        user_id=user_id,
        org_id=org_id,
        role=new_role,
        joined_at=datetime.fromisoformat(joined) if joined else datetime.now(timezone.utc),
    )


async def remove_member(org_id: str, user_id: str, remover_id: str) -> None:
    """Remove a user from an org (Design §2.2).

    Raises NotFoundError if the membership doesn't exist. Logs ``member.removed``.
    Note: callers must prevent removing the last owner. Performance: < 100ms.
    """
    try:
        await clients.run_aws(
            clients.dynamodb().delete_item,
            TableName=_table(),
            Key=to_item({"PK": _user_pk(user_id), "SK": _org_pk(org_id)}),
            ConditionExpression="attribute_exists(PK)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise NotFoundError(
                f"No membership for user {user_id} in org {org_id}"
            ) from exc
        raise MembershipError(f"Failed to remove member: {exc}") from exc

    await log_audit(org_id, remover_id, ActionType.MEMBER_REMOVED, "user", user_id)
    log.info("member.removed", extra={"org_id": org_id, "user_id": user_id})


async def create_user_if_not_exists(user_id: str, email: str) -> None:
    """Idempotently create a user record (Design §2.2).

    Called by the Cognito post-confirm Lambda; safe to call twice (conditional
    write). Performance: < 50ms.
    """
    now = datetime.now(timezone.utc)
    try:
        await clients.run_aws(
            clients.dynamodb().put_item,
            TableName=_table(),
            Item=to_item({"PK": _user_pk(user_id), "SK": "METADATA",
                          "email": email, "created_at": now.isoformat()}),
            ConditionExpression="attribute_not_exists(PK)",
        )
        log.info("user.created", extra={"user_id": user_id})
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # already exists — no-op
        raise MembershipError(f"Failed to create user: {exc}") from exc


def _membership_item(
    org_id: str, user_id: str, role: Role, now: datetime
) -> dict[str, object]:
    return {
        "PK": _user_pk(user_id),
        "SK": _org_pk(org_id),
        "GSI1PK": _org_pk(org_id),
        "GSI1SK": _user_pk(user_id),
        "role": role.value,
        "joined_at": now.isoformat(),
    }
