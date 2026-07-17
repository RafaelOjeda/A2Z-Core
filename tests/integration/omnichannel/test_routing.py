"""Integration tests for assignment/routing (§5.3, Build Order Step 6).

Runs against real Postgres (conversation_assignments rows) and real Core
settings (moto DynamoDB + fakeredis, via the ``aws`` fixture) so
``set_routing_config``'s metadata round-trip is genuinely exercised, not
mocked. ``core.membership.get_membership`` is stubbed at the module's own
import site (``routing.get_membership``) since seeding Core's membership
table is orthogonal to what this module is responsible for -- same pattern
Step 5's tests used for ``handlers.get_membership``. ``core.audit.log_audit``
and ``core.realtime.publish_update`` are mocked too, for the same reason
Step 5 mocked ``worker.publish_event``/``worker.publish_update``: Core's own
suite already covers their internals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.membership import Membership, Role
from app.services.omnichannel import routing, worker
from app.services.omnichannel.exceptions import (
    ConversationAlreadyAssignedError,
    ConversationNotFoundError,
    ForbiddenError,
    RoutingError,
)
from app.services.omnichannel.models import ChannelIdentity, Conversation, ConversationAssignment

pytestmark = pytest.mark.integration


def _membership(org_id: str, role: Role) -> Membership:
    return Membership(
        user_id="whoever", org_id=org_id, role=role, joined_at=datetime.now(timezone.utc)
    )


def _stub_membership(monkeypatch: pytest.MonkeyPatch, role: Role, org_id: str = "org-a") -> None:
    monkeypatch.setattr(
        routing, "get_membership", AsyncMock(return_value=_membership(org_id, role))
    )


def _mock_audit_and_realtime(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    """Mock the Core fan-out calls _record_assignment makes (audit/event/realtime).

    Returns the audit + realtime mocks; see ``_mock_core_calls`` when a test
    needs the ``publish_event`` mock too.
    """
    mock_audit, mock_event, mock_realtime = _mock_core_calls(monkeypatch)
    return mock_audit, mock_realtime


def _mock_core_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """Mock log_audit / publish_event / publish_update at routing's import site."""
    mock_audit = AsyncMock()
    mock_event = AsyncMock()
    mock_realtime = AsyncMock()
    monkeypatch.setattr(routing, "log_audit", mock_audit)
    monkeypatch.setattr(routing, "publish_event", mock_event)
    monkeypatch.setattr(routing, "publish_update", mock_realtime)
    return mock_audit, mock_event, mock_realtime


async def _seed_conversation(session: AsyncSession, org_id: str = "org-a") -> Conversation:
    identity = ChannelIdentity(org_id=org_id, channel_type="whatsapp", external_id="15551234567")
    session.add(identity)
    await session.flush()
    conversation = Conversation(org_id=org_id, customer_identity_id=identity.id, status="open")
    session.add(conversation)
    await session.commit()
    return conversation


# --- claim ---


async def test_claim_assigns_and_records_history(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.MEMBER)
    mock_audit, mock_event, mock_realtime = _mock_core_calls(monkeypatch)
    conversation = await _seed_conversation(session)

    result = await routing.claim(session, "org-a", conversation.id, "agent-1")

    assert result.assigned_user_id == "agent-1"
    rows = (await session.execute(select(ConversationAssignment))).scalars().all()
    assert len(rows) == 1
    assert rows[0].assigned_user_id == "agent-1"
    assert rows[0].assigned_by == "agent-1"
    assert rows[0].reason == "claim"
    mock_audit.assert_called_once()

    # Fans out to both the org-wide inbox and the assignee's own channel (§5.4).
    assert mock_realtime.await_count == 2
    channels = [call.args[1] for call in mock_realtime.await_args_list]
    assert channels == ["org:org-a:conversations", "user:agent-1:notifications"]


async def test_claim_publishes_conversation_assigned_event(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.1 lists conversation.assigned among this service's published events."""
    _stub_membership(monkeypatch, Role.MEMBER)
    _, mock_event, _ = _mock_core_calls(monkeypatch)
    conversation = await _seed_conversation(session)

    await routing.claim(session, "org-a", conversation.id, "agent-1")

    mock_event.assert_awaited_once()
    args, kwargs = mock_event.await_args
    assert args[0] == "org-a"
    assert args[1] == "conversation.assigned"
    assert args[2]["conversation_id"] == conversation.id
    assert args[2]["assigned_user_id"] == "agent-1"
    assert args[2]["reason"] == "claim"
    assert kwargs["source"] == "a2z.omnichannel"


async def test_claim_idempotent_for_same_user(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.MEMBER)
    _mock_audit_and_realtime(monkeypatch)
    conversation = await _seed_conversation(session)

    await routing.claim(session, "org-a", conversation.id, "agent-1")
    await routing.claim(session, "org-a", conversation.id, "agent-1")

    rows = (await session.execute(select(ConversationAssignment))).scalars().all()
    assert len(rows) == 1  # second claim was a no-op, not a new history row


async def test_claim_already_assigned_to_other_raises(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.MEMBER)
    _mock_audit_and_realtime(monkeypatch)
    conversation = await _seed_conversation(session)
    await routing.claim(session, "org-a", conversation.id, "agent-1")

    with pytest.raises(ConversationAlreadyAssignedError):
        await routing.claim(session, "org-a", conversation.id, "agent-2")


async def test_claim_guest_forbidden(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.GUEST)
    conversation = await _seed_conversation(session)

    with pytest.raises(ForbiddenError):
        await routing.claim(session, "org-a", conversation.id, "viewer-1")


async def test_claim_not_a_member_raises(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routing, "get_membership", AsyncMock(return_value=None))
    conversation = await _seed_conversation(session)

    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await routing.claim(session, "org-a", conversation.id, "stranger")


async def test_claim_unknown_conversation_raises(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.MEMBER)
    with pytest.raises(ConversationNotFoundError):
        await routing.claim(session, "org-a", "does-not-exist", "agent-1")


# --- reassign ---


async def test_reassign_by_admin_succeeds(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    mock_audit, mock_realtime = _mock_audit_and_realtime(monkeypatch)
    conversation = await _seed_conversation(session)
    await routing.claim(session, "org-a", conversation.id, "agent-1")

    result = await routing.reassign(session, "org-a", conversation.id, "admin-1", "agent-2")

    assert result.assigned_user_id == "agent-2"
    rows = (await session.execute(select(ConversationAssignment))).scalars().all()
    assert len(rows) == 2  # claim + reassign, both kept -- append-only
    assert rows[-1].assigned_by == "admin-1"
    assert rows[-1].reason == "reassign"
    assert mock_audit.call_count == 2
    # 2 assignments x 2 channels each (org inbox + the new assignee's own).
    assert mock_realtime.call_count == 4
    assert mock_realtime.call_args.args[1] == "user:agent-2:notifications"


async def test_reassign_by_member_forbidden(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.MEMBER)
    conversation = await _seed_conversation(session)

    with pytest.raises(ForbiddenError):
        await routing.reassign(session, "org-a", conversation.id, "agent-1", "agent-2")


async def test_reassign_to_non_member_raises(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First call (actor) succeeds, second call (assignee) returns None.
    calls = [_membership("org-a", Role.ADMIN), None]
    monkeypatch.setattr(routing, "get_membership", AsyncMock(side_effect=calls))
    conversation = await _seed_conversation(session)

    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await routing.reassign(session, "org-a", conversation.id, "admin-1", "not-a-member")


# --- single-assignee routing strategy ---


async def test_single_assignee_strategy_auto_assigns_new_conversation(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.OWNER)
    _mock_audit_and_realtime(monkeypatch)

    await routing.set_routing_config("org-a", "owner-1", "single_assignee", "agent-1")

    identity = ChannelIdentity(org_id="org-a", channel_type="whatsapp", external_id="15551234567")
    session.add(identity)
    await session.flush()
    conversation, is_new = await worker._find_or_create_conversation(session, "org-a", identity.id)
    assert is_new is True

    await routing.apply_single_assignee_if_configured(session, conversation)

    assert conversation.assigned_user_id == "agent-1"
    rows = (await session.execute(select(ConversationAssignment))).scalars().all()
    assert len(rows) == 1
    assert rows[0].assigned_by == "routing:single_assignee"


async def test_single_assignee_not_configured_leaves_unassigned(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_audit_and_realtime(monkeypatch)
    conversation = await _seed_conversation(session)

    await routing.apply_single_assignee_if_configured(session, conversation)

    assert conversation.assigned_user_id is None
    rows = (await session.execute(select(ConversationAssignment))).scalars().all()
    assert len(rows) == 0


async def test_set_routing_config_rejects_unsupported_strategy(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.OWNER)
    with pytest.raises(RoutingError):
        await routing.set_routing_config("org-a", "owner-1", "round_robin")


async def test_set_routing_config_requires_designated_user(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.OWNER)
    with pytest.raises(RoutingError):
        await routing.set_routing_config("org-a", "owner-1", "single_assignee")


async def test_set_routing_config_forbidden_for_member(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.MEMBER)
    with pytest.raises(ForbiddenError):
        await routing.set_routing_config("org-a", "agent-1", "single_assignee", "agent-1")


# --- worker wiring ---


async def test_worker_notifies_assignee_channel_when_single_assignee_configured(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: an inbound message on a fresh conversation auto-assigns
    and the worker pushes a realtime update to the assignee's own channel,
    on top of the org-wide inbox update (§5.6: "-> notify assignee")."""
    _stub_membership(monkeypatch, Role.OWNER)
    monkeypatch.setattr(routing, "log_audit", AsyncMock())
    await routing.set_routing_config("org-a", "owner-1", "single_assignee", "agent-1")

    mock_publish_event = AsyncMock()
    mock_publish_update = AsyncMock()
    monkeypatch.setattr(worker, "publish_event", mock_publish_event)
    monkeypatch.setattr(worker, "publish_update", mock_publish_update)
    monkeypatch.setattr(routing, "publish_update", mock_publish_update)

    from app.services.omnichannel import queues

    async def _fake_receive(*, max_messages: int = 10, wait_time_seconds: int = 0):  # type: ignore[no-untyped-def]
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [],
                                "messages": [
                                    {
                                        "from": "15551234567",
                                        "id": "wamid.X",
                                        "type": "text",
                                        "text": {"body": "hi"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        return [
            queues.QueueMessage(
                body={"raw_payload": payload},
                attributes={"org_id": "org-a", "channel_type": "whatsapp"},
                receipt_handle="fake",
                receive_count=1,
            )
        ]

    monkeypatch.setattr(queues, "receive_inbound", _fake_receive)
    monkeypatch.setattr(queues, "delete_inbound", AsyncMock())

    processed = await worker.process_inbound_batch(session)
    assert processed == 1

    conversations = (await session.execute(select(Conversation))).scalars().all()
    assert len(conversations) == 1
    assert conversations[0].assigned_user_id == "agent-1"

    # org-wide update (message.received) + assignee-channel update, plus the
    # one realtime publish from inside _record_assignment itself.
    channels = [call.args[1] for call in mock_publish_update.call_args_list]
    assert "user:agent-1:notifications" in channels
    assert "org:org-a:conversations" in channels
