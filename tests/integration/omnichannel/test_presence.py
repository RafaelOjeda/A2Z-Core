"""Tests for Redis-backed agent presence (§5.3)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.services.omnichannel import db, presence
from app.services.omnichannel.models import Presence

pytestmark = pytest.mark.integration


async def test_heartbeat_marks_online() -> None:
    await presence.heartbeat("org-a", "user-1")
    assert await presence.get_status("org-a", "user-1") == "online"


async def test_default_status_is_offline() -> None:
    assert await presence.get_status("org-a", "user-never-seen") == "offline"


async def test_heartbeat_writes_postgres_backup_row() -> None:
    await presence.heartbeat("org-a", "user-1", status="away")

    async with db.get_session_context() as session:
        row = (
            await session.execute(
                select(Presence).where(Presence.org_id == "org-a", Presence.user_id == "user-1")
            )
        ).scalar_one()
        assert row.status == "away"


async def test_heartbeat_upserts_backup_row_on_repeat_calls() -> None:
    await presence.heartbeat("org-a", "user-1", status="online")
    await presence.heartbeat("org-a", "user-1", status="away")

    async with db.get_session_context() as session:
        rows = (
            (
                await session.execute(
                    select(Presence).where(Presence.org_id == "org-a", Presence.user_id == "user-1")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == "away"


async def test_list_online_agents_filters_candidates() -> None:
    await presence.heartbeat("org-a", "user-online")

    online = await presence.list_online_agents(
        "org-a", ["user-online", "user-offline", "user-never-seen"]
    )
    assert online == ["user-online"]


async def test_list_online_agents_scoped_by_org() -> None:
    await presence.heartbeat("org-a", "user-1")

    # Same user id, different org: no heartbeat there.
    online = await presence.list_online_agents("org-b", ["user-1"])
    assert online == []


async def test_list_online_agents_empty_candidates_short_circuits() -> None:
    assert await presence.list_online_agents("org-a", []) == []
