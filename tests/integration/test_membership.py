"""Integration tests for core.membership against moto, incl. Design §4.1 scenario."""

from __future__ import annotations

import pytest

from app.core import audit, membership
from app.core.audit import ActionType
from app.core.exceptions import AlreadyExistsError, NotFoundError
from app.core.membership import Role

pytestmark = pytest.mark.integration


async def test_create_org_and_manage_members(aws: None) -> None:
    """Design §4.1: create org -> list -> add -> change role -> audit trail."""
    owner = "auth0|owner123"
    await membership.create_user_if_not_exists(owner, "owner@acme.com")

    org = await membership.create_org("Acme Jewelry", owner)
    assert org.org_id
    assert org.owner_id == owner

    m = await membership.get_membership(owner, org.org_id)
    assert m is not None and m.role == Role.OWNER

    members = await membership.list_org_members(org.org_id)
    assert len(members) == 1 and members[0].user_id == owner

    member = "auth0|member456"
    await membership.create_user_if_not_exists(member, "sarah@acme.com")
    added = await membership.add_member(org.org_id, member, Role.MEMBER, owner)
    assert added.role == Role.MEMBER

    members = await membership.list_org_members(org.org_id)
    assert len(members) == 2

    updated = await membership.change_role(org.org_id, member, Role.ADMIN, owner)
    assert updated.role == Role.ADMIN

    events = await audit.get_audit_events(
        org.org_id, action_type=ActionType.MEMBER_ROLE_CHANGED, resource_id=member
    )
    assert len(events) >= 1
    assert events[0].metadata["new_role"] == "admin"
    assert events[0].metadata["old_role"] == "member"


async def test_get_membership_none_when_absent(aws: None) -> None:
    assert await membership.get_membership("nobody", "no-org") is None


async def test_add_duplicate_member_raises(aws: None) -> None:
    org = await membership.create_org("Org", "owner")
    with pytest.raises(AlreadyExistsError):
        await membership.add_member(org.org_id, "owner", Role.ADMIN, "owner")


async def test_change_role_missing_raises(aws: None) -> None:
    with pytest.raises(NotFoundError):
        await membership.change_role("org-x", "ghost", Role.ADMIN, "admin")


async def test_remove_member(aws: None) -> None:
    org = await membership.create_org("Org", "owner")
    await membership.add_member(org.org_id, "u2", Role.MEMBER, "owner")
    await membership.remove_member(org.org_id, "u2", "owner")
    assert await membership.get_membership("u2", org.org_id) is None
    with pytest.raises(NotFoundError):
        await membership.remove_member(org.org_id, "u2", "owner")


async def test_create_user_is_idempotent(aws: None) -> None:
    await membership.create_user_if_not_exists("u", "u@x.com")
    # Second call must be a no-op, not an error.
    await membership.create_user_if_not_exists("u", "u@x.com")


async def test_list_user_orgs(aws: None) -> None:
    await membership.create_user_if_not_exists("multi", "m@x.com")
    a = await membership.create_org("Org A", "multi")
    b = await membership.create_org("Org B", "multi")
    orgs = await membership.list_user_orgs("multi")
    assert {o.org_id for o in orgs} == {a.org_id, b.org_id}


async def test_members_sorted_owner_first(aws: None) -> None:
    org = await membership.create_org("Org", "owner")
    await membership.add_member(org.org_id, "g", Role.GUEST, "owner")
    await membership.add_member(org.org_id, "a", Role.ADMIN, "owner")
    members = await membership.list_org_members(org.org_id)
    assert [m.role for m in members][0] == Role.OWNER
    assert members[-1].role == Role.GUEST


async def test_cross_org_membership_isolation(aws: None) -> None:
    org_a = await membership.create_org("A", "owner-a")
    org_b = await membership.create_org("B", "owner-b")
    # owner-a is not a member of org-b.
    assert await membership.get_membership("owner-a", org_b.org_id) is None
    assert await membership.get_membership("owner-b", org_a.org_id) is None
