"""Integration tests for core.audit against moto DynamoDB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import audit
from app.core.audit import ActionType

pytestmark = pytest.mark.integration


async def test_log_audit_returns_event(aws: None) -> None:
    event = await audit.log_audit(
        org_id="org-a",
        actor_id="user-1",
        action=ActionType.ORG_CREATED,
        resource_type="org",
        resource_id="org-a",
        metadata={"name": "Acme"},
    )
    assert event.event_id
    assert event.action == "org.created"
    assert event.metadata["name"] == "Acme"


async def test_query_by_action_and_actor(aws: None) -> None:
    await audit.log_audit("org-a", "user-1", ActionType.MEMBER_ADDED, "user", "u2")
    await audit.log_audit("org-a", "user-1", ActionType.SETTINGS_CHANGED, "settings", "org-a")

    added = await audit.get_audit_events("org-a", action_type=ActionType.MEMBER_ADDED)
    assert len(added) == 1
    assert added[0].resource_id == "u2"

    by_actor = await audit.get_audit_events("org-a", actor_id="user-1")
    assert len(by_actor) == 2


async def test_query_by_resource_id(aws: None) -> None:
    await audit.log_audit("org-a", "user-1", ActionType.MEMBER_ROLE_CHANGED, "user", "target")
    events = await audit.get_audit_events("org-a", resource_id="target")
    assert len(events) == 1


async def test_newest_first_ordering(aws: None) -> None:
    await audit.log_audit("org-a", "u", "evt.one", "x", "1")
    await audit.log_audit("org-a", "u", "evt.two", "x", "2")
    events = await audit.get_audit_events("org-a")
    assert [e.resource_id for e in events] == ["2", "1"]


async def test_time_range_filter(aws: None) -> None:
    await audit.log_audit("org-a", "u", "evt", "x", "1")
    now = datetime.now(timezone.utc)
    future = await audit.get_audit_events("org-a", from_time=now + timedelta(hours=1))
    assert future == []
    recent = await audit.get_audit_events("org-a", from_time=now - timedelta(hours=1))
    assert len(recent) == 1


async def test_cross_org_isolation(aws: None) -> None:
    await audit.log_audit("org-a", "u", ActionType.ORG_CREATED, "org", "org-a")
    await audit.log_audit("org-b", "u", ActionType.ORG_CREATED, "org", "org-b")
    only_b = await audit.get_audit_events("org-b")
    assert len(only_b) == 1
    assert only_b[0].org_id == "org-b"
