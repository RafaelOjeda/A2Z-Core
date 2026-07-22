"""Integration tests for inbox reads (§3, §5.1 -- Build Order Step 9).

Real Postgres for the queries, real moto S3 + fakeredis for the signed
attachment URLs. ``core.membership.get_membership`` is stubbed at ``access`` -- the single
seam the authz gate resolves membership through, matching what the
routing/handler suites do -- since seeding Core's membership table is
orthogonal to what this module owns.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.membership import Membership, Role
from app.services.omnichannel import inbox
from app.services.omnichannel.exceptions import ConversationNotFoundError, InvalidQueryError
from app.services.omnichannel.models import (
    ChannelIdentity,
    Conversation,
    Message,
    MessageAttachment,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


def _stub_membership(
    monkeypatch: pytest.MonkeyPatch, role: Role | None = Role.MEMBER, org_id: str = "org-a"
) -> None:
    value = (
        None
        if role is None
        else Membership(user_id="agent-1", org_id=org_id, role=role, joined_at=_NOW)
    )
    monkeypatch.setattr(access, "get_membership", AsyncMock(return_value=value))


async def _seed_conversation(
    session: AsyncSession,
    org_id: str = "org-a",
    *,
    external_id: str = "15551234567",
    status: str = "open",
    assigned_user_id: str | None = None,
    last_message_at: datetime | None = _NOW,
    unread_count: int = 0,
) -> Conversation:
    identity = ChannelIdentity(
        org_id=org_id,
        channel_type="whatsapp",
        external_id=external_id,
        display_name="Jane",
    )
    session.add(identity)
    await session.flush()
    conversation = Conversation(
        org_id=org_id,
        customer_identity_id=identity.id,
        status=status,
        assigned_user_id=assigned_user_id,
        last_message_at=last_message_at,
        last_message_preview="hi",
        unread_count=unread_count,
    )
    session.add(conversation)
    await session.commit()
    return conversation


async def _seed_message(
    session: AsyncSession,
    conversation: Conversation,
    *,
    body: str,
    created_at: datetime,
    external_message_id: str,
    direction: str = "inbound",
) -> Message:
    message = Message(
        org_id=conversation.org_id,
        conversation_id=conversation.id,
        direction=direction,
        channel_type="whatsapp",
        external_message_id=external_message_id,
        body_text=body,
        content_type="text/plain",
        status="received",
        created_at=created_at,
    )
    session.add(message)
    await session.commit()
    return message


# --- list_conversations ---


async def test_list_returns_org_conversations(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    await _seed_conversation(session, external_id="1111")

    page = await inbox.list_conversations(session, "org-a", "agent-1")

    assert len(page.items) == 1
    assert page.items[0].customer_external_id == "1111"
    assert page.items[0].customer_display_name == "Jane"
    assert page.items[0].channel_type == "whatsapp"
    assert page.next_cursor is None


async def test_list_orders_by_most_recent_activity(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    await _seed_conversation(session, external_id="old", last_message_at=_NOW - timedelta(hours=2))
    await _seed_conversation(session, external_id="newest", last_message_at=_NOW)
    await _seed_conversation(session, external_id="mid", last_message_at=_NOW - timedelta(hours=1))

    page = await inbox.list_conversations(session, "org-a", "agent-1")

    assert [c.customer_external_id for c in page.items] == ["newest", "mid", "old"]


async def test_list_ascending_sort(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_membership(monkeypatch)
    await _seed_conversation(session, external_id="old", last_message_at=_NOW - timedelta(hours=2))
    await _seed_conversation(session, external_id="newest", last_message_at=_NOW)
    await _seed_conversation(session, external_id="mid", last_message_at=_NOW - timedelta(hours=1))

    page = await inbox.list_conversations(session, "org-a", "agent-1", sort="last_message_at")

    assert [c.customer_external_id for c in page.items] == ["old", "mid", "newest"]


async def test_list_rejects_unknown_sort(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)

    with pytest.raises(InvalidQueryError):
        await inbox.list_conversations(session, "org-a", "agent-1", sort="bogus")


async def test_list_puts_never_active_conversations_last(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nullslast(): a conversation with no messages must not outrank live ones."""
    _stub_membership(monkeypatch)
    await _seed_conversation(session, external_id="never", last_message_at=None)
    await _seed_conversation(session, external_id="live", last_message_at=_NOW)

    page = await inbox.list_conversations(session, "org-a", "agent-1")

    assert [c.customer_external_id for c in page.items] == ["live", "never"]


async def test_list_puts_never_active_conversations_last_ascending(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NULLS LAST holds in either sort direction -- "never active" always sinks."""
    _stub_membership(monkeypatch)
    await _seed_conversation(session, external_id="never", last_message_at=None)
    await _seed_conversation(session, external_id="live", last_message_at=_NOW)

    page = await inbox.list_conversations(session, "org-a", "agent-1", sort="last_message_at")

    assert [c.customer_external_id for c in page.items] == ["live", "never"]


async def test_list_filters_by_status(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    await _seed_conversation(session, external_id="open-1", status="open")
    await _seed_conversation(session, external_id="closed-1", status="closed")

    page = await inbox.list_conversations(session, "org-a", "agent-1", status="open")

    assert [c.customer_external_id for c in page.items] == ["open-1"]


async def test_list_filters_by_assignee(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    await _seed_conversation(session, external_id="mine", assigned_user_id="agent-1")
    await _seed_conversation(session, external_id="theirs", assigned_user_id="agent-2")
    await _seed_conversation(session, external_id="unassigned")

    page = await inbox.list_conversations(session, "org-a", "agent-1", assigned_user_id="agent-1")

    assert [c.customer_external_id for c in page.items] == ["mine"]


async def test_list_search_matches_customer_name(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    await _seed_conversation(session, external_id="1111")  # display_name="Jane"
    conv2 = await _seed_conversation(session, external_id="2222")
    identity2 = await session.get(ChannelIdentity, conv2.customer_identity_id)
    assert identity2 is not None
    identity2.display_name = "Bob"
    await session.commit()

    page = await inbox.list_conversations(session, "org-a", "agent-1", q="jane")

    assert [c.customer_external_id for c in page.items] == ["1111"]


async def test_list_search_matches_message_body(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    matching = await _seed_conversation(session, external_id="has-invoice")
    other = await _seed_conversation(session, external_id="no-match")
    await _seed_message(
        session, matching, body="where is my invoice?", created_at=_NOW, external_message_id="m1"
    )
    await _seed_message(
        session, other, body="what's the weather", created_at=_NOW, external_message_id="m2"
    )

    page = await inbox.list_conversations(session, "org-a", "agent-1", q="invoice")

    assert [c.customer_external_id for c in page.items] == ["has-invoice"]


async def test_list_search_is_org_scoped(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    mine = await _seed_conversation(session, org_id="org-a", external_id="mine")
    theirs = await _seed_conversation(session, org_id="org-b", external_id="theirs")
    await _seed_message(
        session, mine, body="secret sauce", created_at=_NOW, external_message_id="m1"
    )
    await _seed_message(
        session, theirs, body="secret sauce", created_at=_NOW, external_message_id="m2"
    )

    page = await inbox.list_conversations(session, "org-a", "agent-1", q="secret")

    assert [c.customer_external_id for c in page.items] == ["mine"]


async def test_list_cursor_walks_all_pages_without_duplicates_or_gaps(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    for i in range(5):
        await _seed_conversation(
            session, external_id=f"c{i}", last_message_at=_NOW - timedelta(minutes=i)
        )
    # One never-active conversation -- must still appear, at the very end.
    await _seed_conversation(session, external_id="never", last_message_at=None)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # generous bound against an infinite-loop bug
        page = await inbox.list_conversations(session, "org-a", "agent-1", limit=2, cursor=cursor)
        seen.extend(c.customer_external_id for c in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    else:
        pytest.fail("cursor walk did not terminate")

    assert seen == ["c0", "c1", "c2", "c3", "c4", "never"]


async def test_list_cursor_stable_under_concurrent_insert(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike offset, a keyset cursor must not skip/repeat rows when a new
    conversation is inserted between page reads."""
    _stub_membership(monkeypatch)
    for i in range(3):
        await _seed_conversation(
            session, external_id=f"c{i}", last_message_at=_NOW - timedelta(minutes=i)
        )

    page1 = await inbox.list_conversations(session, "org-a", "agent-1", limit=2)
    assert [c.customer_external_id for c in page1.items] == ["c0", "c1"]

    # A brand-new, most-recent conversation arrives after page1 was read.
    await _seed_conversation(
        session, external_id="brand-new", last_message_at=_NOW + timedelta(minutes=1)
    )

    page2 = await inbox.list_conversations(
        session, "org-a", "agent-1", limit=2, cursor=page1.next_cursor
    )
    # Continues strictly after c1, unaffected by the new row landing ahead of it.
    assert [c.customer_external_id for c in page2.items] == ["c2"]
    assert page2.next_cursor is None


async def test_list_rejects_malformed_cursor(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)

    with pytest.raises(InvalidQueryError):
        await inbox.list_conversations(session, "org-a", "agent-1", cursor="not-a-cursor")


async def test_list_clamps_limit(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_membership(monkeypatch)
    await _seed_conversation(session)

    # Asking for more than MAX_LIMIT must not let a caller drain the org.
    page = await inbox.list_conversations(session, "org-a", "agent-1", limit=10_000)
    assert len(page.items) == 1


async def test_list_cross_org_isolation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    await _seed_conversation(session, org_id="org-a", external_id="a-customer")
    await _seed_conversation(session, org_id="org-b", external_id="b-customer")

    page = await inbox.list_conversations(session, "org-a", "agent-1")

    assert [c.customer_external_id for c in page.items] == ["a-customer"]


async def test_list_requires_membership(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, role=None)
    await _seed_conversation(session)

    with pytest.raises(NotFoundError):
        await inbox.list_conversations(session, "org-a", "stranger")


async def test_viewer_may_read(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """§4: every role reads, unlike send/claim which exclude Viewer (GUEST)."""
    _stub_membership(monkeypatch, role=Role.GUEST)
    await _seed_conversation(session)

    assert len((await inbox.list_conversations(session, "org-a", "viewer-1")).items) == 1


# --- get_conversation ---


async def test_get_returns_thread_in_reading_order(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    conversation = await _seed_conversation(session)
    await _seed_message(
        session, conversation, body="first", created_at=_NOW, external_message_id="m1"
    )
    await _seed_message(
        session,
        conversation,
        body="second",
        created_at=_NOW + timedelta(minutes=1),
        external_message_id="m2",
    )

    detail = await inbox.get_conversation(session, "org-a", conversation.id, "agent-1")

    assert [m.body_text for m in detail.messages] == ["first", "second"]
    assert detail.conversation.id == conversation.id


async def test_get_returns_tail_of_long_thread(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A limited thread read must return the *newest* messages, still in order."""
    _stub_membership(monkeypatch)
    conversation = await _seed_conversation(session)
    for i in range(5):
        await _seed_message(
            session,
            conversation,
            body=f"msg-{i}",
            created_at=_NOW + timedelta(minutes=i),
            external_message_id=f"m{i}",
        )

    detail = await inbox.get_conversation(session, "org-a", conversation.id, "agent-1", limit=2)

    assert [m.body_text for m in detail.messages] == ["msg-3", "msg-4"]
    assert detail.messages_next_cursor is not None


async def test_get_messages_next_cursor_absent_on_last_page(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    conversation = await _seed_conversation(session)
    await _seed_message(
        session, conversation, body="only one", created_at=_NOW, external_message_id="m1"
    )

    detail = await inbox.get_conversation(session, "org-a", conversation.id, "agent-1", limit=50)

    assert detail.messages_next_cursor is None


async def test_get_before_cursor_walks_full_thread_without_duplicates(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    conversation = await _seed_conversation(session)
    for i in range(5):
        await _seed_message(
            session,
            conversation,
            body=f"msg-{i}",
            created_at=_NOW + timedelta(minutes=i),
            external_message_id=f"m{i}",
        )

    # Newest page first (reading-order tail), then walk backward with `before`.
    seen: list[str] = []
    detail = await inbox.get_conversation(session, "org-a", conversation.id, "agent-1", limit=2)
    seen = [m.body_text for m in detail.messages] + seen
    for _ in range(10):
        if detail.messages_next_cursor is None:
            break
        detail = await inbox.get_conversation(
            session,
            "org-a",
            conversation.id,
            "agent-1",
            limit=2,
            before=detail.messages_next_cursor,
        )
        seen = [m.body_text for m in detail.messages] + seen
    else:
        pytest.fail("before-cursor walk did not terminate")

    assert seen == ["msg-0", "msg-1", "msg-2", "msg-3", "msg-4"]


async def test_get_before_malformed_cursor_raises(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    conversation = await _seed_conversation(session)

    with pytest.raises(InvalidQueryError):
        await inbox.get_conversation(session, "org-a", conversation.id, "agent-1", before="garbage")


async def test_get_includes_signed_attachment_urls(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    conversation = await _seed_conversation(session)
    message = await _seed_message(
        session, conversation, body="see photo", created_at=_NOW, external_message_id="m1"
    )
    session.add(
        MessageAttachment(
            message_id=message.id,
            org_id="org-a",
            s3_key="org-a/omnichannel/20260717-120000-000000_receipt.pdf",
            content_type="application/pdf",
            size_bytes=1234,
        )
    )
    await session.commit()

    detail = await inbox.get_conversation(session, "org-a", conversation.id, "agent-1")

    attachments = detail.messages[0].attachments
    assert len(attachments) == 1
    # Filename is recovered from the key -- there is no filename column (§5.1).
    assert attachments[0].filename == "receipt.pdf"
    assert attachments[0].content_type == "application/pdf"
    assert attachments[0].size_bytes == 1234
    assert attachments[0].url.startswith("http")
    assert "X-Amz-Signature" in attachments[0].url or "Signature" in attachments[0].url


async def test_get_cross_org_conversation_is_not_found(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    other = await _seed_conversation(session, org_id="org-b", external_id="b-customer")

    with pytest.raises(ConversationNotFoundError):
        await inbox.get_conversation(session, "org-a", other.id, "agent-1")


async def test_get_unknown_conversation_is_not_found(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)

    with pytest.raises(ConversationNotFoundError):
        await inbox.get_conversation(session, "org-a", "does-not-exist", "agent-1")


async def test_get_requires_membership(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, role=None)
    conversation = await _seed_conversation(session)

    with pytest.raises(NotFoundError):
        await inbox.get_conversation(session, "org-a", conversation.id, "stranger")


# --- mark_read ---


async def test_mark_read_zeroes_unread_count(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    conversation = await _seed_conversation(session, unread_count=7)

    result = await inbox.mark_read(session, "org-a", conversation.id, "agent-1")

    assert result.unread_count == 0
    await session.refresh(conversation)
    assert conversation.unread_count == 0


async def test_get_conversation_does_not_mark_read(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading must not clear the badge -- a prefetch would silently do it."""
    _stub_membership(monkeypatch)
    conversation = await _seed_conversation(session, unread_count=3)

    detail = await inbox.get_conversation(session, "org-a", conversation.id, "agent-1")

    assert detail.conversation.unread_count == 3
    await session.refresh(conversation)
    assert conversation.unread_count == 3


async def test_mark_read_cross_org_is_not_found(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch)
    other = await _seed_conversation(session, org_id="org-b", external_id="b-customer")

    with pytest.raises(ConversationNotFoundError):
        await inbox.mark_read(session, "org-a", other.id, "agent-1")


# --- index usage (§5.1) ---


async def _explain(session: AsyncSession, sql: str) -> list[str]:
    # enable_seqscan=off so the plan is meaningful on a small test table --
    # otherwise Postgres seq-scans regardless and the plan proves nothing.
    await session.execute(text("SET enable_seqscan = off"))
    return list((await session.execute(text(f"EXPLAIN {sql}"))).scalars().all())


async def test_inbox_query_uses_index_without_sorting(session: AsyncSession) -> None:
    """ix_conversations_inbox must fully serve the inbox ORDER BY (§5.1).

    The index is (org_id, status, last_message_at DESC NULLS LAST) so a btree
    scan yields rows already ordered. If someone "simplifies" it back to plain
    ASC -- or drops NULLS LAST from either side -- the planner silently adds a
    Sort over every matching conversation, which is what this catches.
    """
    plan = await _explain(
        session,
        "SELECT * FROM omnichannel.conversations WHERE org_id='org-a' AND status='open' "
        "ORDER BY last_message_at DESC NULLS LAST LIMIT 50",
    )

    assert any("ix_conversations_inbox" in line for line in plan), plan
    assert not any("Sort" in line for line in plan), f"index no longer serves the ORDER BY: {plan}"


@pytest.mark.parametrize(
    ("index_name", "expected_columns"),
    [
        # §5.1's index list, asserted as definitions rather than as planner
        # choices: *which* index the planner picks between viable candidates is
        # a cost decision that needs representative data to be meaningful (on an
        # empty table it will happily pick ix_conversations_inbox with a Filter
        # for the agent query -- correct, just not what the index list intends).
        # Definitions are deterministic at any size, and catch the thing worth
        # catching: an index being dropped or silently re-specified.
        ("ix_conversations_inbox", "(org_id, status, last_message_at DESC NULLS LAST)"),
        ("ix_conversations_agent_inbox", "(org_id, assigned_user_id, status)"),
        ("ix_messages_thread", "(conversation_id, created_at)"),
    ],
)
async def test_index_definitions_match_spec(
    session: AsyncSession, index_name: str, expected_columns: str
) -> None:
    row = (
        await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE schemaname='omnichannel' AND indexname=:n"),
            {"n": index_name},
        )
    ).scalar_one_or_none()

    assert row is not None, f"{index_name} is missing"
    assert expected_columns in row, f"{index_name} definition drifted: {row}"
