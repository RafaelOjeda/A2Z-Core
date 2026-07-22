"""Inbox reads -- listing conversations and reading a thread (§3, §5.1).

The counterpart to the write paths in ``handlers.py``/``routing.py``: this is
what actually makes the product "a unified inbox" rather than a pipeline that
ingests messages nobody can see. Every query here is deliberately shaped to
the indexes §5.1 already specifies:

  * ``ix_conversations_inbox``       (org_id, status, last_message_at) -- the inbox
  * ``ix_conversations_agent_inbox`` (org_id, assigned_user_id, status) -- "mine"
  * ``ix_messages_thread``           (conversation_id, created_at) -- the thread

**Authz differs from the write paths.** §4 grants *every* role -- Owner, Admin,
Agent and Viewer -- read access to all of the org's conversations, so these
only require membership. ``handlers.send_reply`` / ``routing.claim`` exclude
GUEST (Viewer); reading deliberately does not.

**Pagination is keyset (cursor), not offset (API review, 2026-07-18).** A
bare ``offset`` drifts under concurrent inserts (a new inbound message
shifts every later page by one) and its cost grows with page depth. The
cursor encodes ``(last_message_at, id)`` -- the exact tuple the ORDER BY
sorts on -- so "next page" means "continue past this row," not "skip N."
``id`` is the tiebreaker (timestamps can collide) and is not part of
``ix_conversations_inbox`` itself; ties are rare enough in practice that
Postgres serves the tiebreak with an Incremental Sort over the index's
output rather than a full Sort, so §5.1's "no Sort" invariant for the
common case still holds -- see ``test_inbox_query_uses_index_without_sorting``.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.services.omnichannel import access, media
from app.services.omnichannel.exceptions import ConversationNotFoundError, InvalidQueryError
from app.services.omnichannel.models import (
    ChannelIdentity,
    Conversation,
    Message,
    MessageAttachment,
)

# Bounded so a caller can't ask for an org's entire history in one request.
MAX_LIMIT = 100
DEFAULT_CONVERSATION_LIMIT = 50
DEFAULT_MESSAGE_LIMIT = 50

_SORT_FIELDS = ("-last_message_at", "last_message_at")


class AttachmentView(BaseModel):
    id: str
    filename: str
    content_type: str
    size_bytes: int
    url: str


class MessageView(BaseModel):
    id: str
    direction: str
    channel_type: str
    body_text: str | None
    content_type: str
    status: str
    sent_by_user_id: str | None
    external_message_id: str
    created_at: datetime
    attachments: list[AttachmentView] = []


class ConversationSummary(BaseModel):
    id: str
    status: str
    assigned_user_id: str | None
    last_message_at: datetime | None
    last_message_preview: str | None
    unread_count: int
    channel_type: str
    customer_external_id: str
    customer_display_name: str | None


class ConversationDetail(BaseModel):
    conversation: ConversationSummary
    messages: list[MessageView]
    # Set when older messages exist beyond `messages`; pass back as `before`
    # to page further into the thread's history.
    messages_next_cursor: str | None = None


class ConversationPage(BaseModel):
    items: list[ConversationSummary]
    # Set when more conversations exist beyond `items`; pass back as `cursor`
    # to fetch the next page. `None` means this was the last page.
    next_cursor: str | None = None


def _encode_cursor(ts: datetime | None, id_: str) -> str:
    raw = json.dumps([ts.isoformat() if ts is not None else None, id_]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime | None, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        ts_raw, id_ = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        ts = datetime.fromisoformat(ts_raw) if ts_raw is not None else None
    except Exception as exc:
        raise InvalidQueryError(f"Malformed cursor: {cursor!r}") from exc
    if not isinstance(id_, str):
        raise InvalidQueryError(f"Malformed cursor: {cursor!r}")
    return ts, id_


def _filename_from_key(s3_key: str) -> str:
    """Recover the display filename from an S3 key.

    ``core.storage.upload_file`` builds keys as
    ``{org_id}/{service_type}/{timestamp}_{filename}``; ``message_attachments``
    has no filename column of its own, so it is derived here rather than
    duplicated. Falls back to the raw basename if the key doesn't carry the
    timestamp prefix (e.g. an object written by something else).
    """
    basename = s3_key.rsplit("/", 1)[-1]
    _, sep, tail = basename.partition("_")
    return tail if sep and tail else basename


def _clamp(limit: int, default: int) -> int:
    if limit <= 0:
        return default
    return min(limit, MAX_LIMIT)


def _summary(conversation: Conversation, identity: ChannelIdentity) -> ConversationSummary:
    return ConversationSummary(
        id=conversation.id,
        status=conversation.status,
        assigned_user_id=conversation.assigned_user_id,
        last_message_at=conversation.last_message_at,
        last_message_preview=conversation.last_message_preview,
        unread_count=conversation.unread_count,
        channel_type=identity.channel_type,
        customer_external_id=identity.external_id,
        customer_display_name=identity.display_name,
    )


def _message_view(message: Message, attachments: list[AttachmentView]) -> MessageView:
    """Build a message DTO -- the thread-view counterpart to :func:`_summary`."""
    return MessageView(
        id=message.id,
        direction=message.direction,
        channel_type=message.channel_type,
        body_text=message.body_text,
        content_type=message.content_type,
        status=message.status,
        sent_by_user_id=message.sent_by_user_id,
        external_message_id=message.external_message_id,
        created_at=message.created_at,
        attachments=attachments,
    )


def _cursor_predicate(*, sort_desc: bool, ts: datetime | None, id_: str) -> ColumnElement[bool]:
    """Continuation predicate for keyset pagination over (last_message_at, id).

    Both directions treat NULL ``last_message_at`` as sorting last (matching
    ``list_conversations``' ORDER BY, symmetric in either direction) --
    "never active" conversations belong at the end of the list regardless
    of which way it's sorted.
    """
    col = Conversation.last_message_at
    id_col = Conversation.id
    if sort_desc:
        if ts is not None:
            return or_(col < ts, and_(col == ts, id_col < id_), col.is_(None))
        return and_(col.is_(None), id_col < id_)
    if ts is not None:
        return or_(col > ts, and_(col == ts, id_col > id_))
    return and_(col.is_(None), id_col > id_)


async def list_conversations(
    session: AsyncSession,
    org_id: str,
    user_id: str,
    *,
    status: str | None = None,
    assigned_user_id: str | None = None,
    q: str | None = None,
    sort: str = "-last_message_at",
    limit: int = DEFAULT_CONVERSATION_LIMIT,
    cursor: str | None = None,
) -> ConversationPage:
    """List an org's conversations, most recently active first by default (§3, §5.1).

    Args:
        org_id: Org whose inbox to read (always filtered on -- no cross-org read).
        user_id: Caller; must be a member of ``org_id`` (any role, §4).
        status: Optional ``open``/``pending``/``closed`` filter. Passing one
            lets ``ix_conversations_inbox`` serve the query on its full prefix.
        assigned_user_id: Optional filter for an agent's own inbox
            (``ix_conversations_agent_inbox``).
        q: Optional search -- matches a customer's display name (``ILIKE``)
            or any message body in the thread (Postgres full-text, served by
            the ``ix_messages_fulltext`` GIN index at distribution scale).
        sort: ``"-last_message_at"`` (default, newest first) or
            ``"last_message_at"`` (oldest first). Ascending order still uses
            the DESC-built index for the leading columns but needs an
            Incremental Sort to reverse the tail; rare enough in practice
            (agents overwhelmingly want newest-first) to accept.
        limit: Page size, clamped to ``MAX_LIMIT``.
        cursor: Opaque continuation token from a previous page's
            ``next_cursor``. Omit for the first page.

    Returns:
        A page of conversation summaries plus ``next_cursor`` (``None`` if
        this was the last page).

    Raises:
        NotFoundError: Caller isn't a member of ``org_id``.
        InvalidQueryError: Unknown ``sort`` value, or a malformed ``cursor``.

    Performance: < 100ms -- one indexed query plus the identity join.
    """
    await access.require_membership(user_id, org_id)

    if sort not in _SORT_FIELDS:
        raise InvalidQueryError(f"Unsupported sort {sort!r}; use one of {_SORT_FIELDS}")
    sort_desc = sort == "-last_message_at"

    stmt = (
        select(Conversation, ChannelIdentity)
        .join(ChannelIdentity, Conversation.customer_identity_id == ChannelIdentity.id)
        .where(Conversation.org_id == org_id)
    )
    if status is not None:
        stmt = stmt.where(Conversation.status == status)
    if assigned_user_id is not None:
        stmt = stmt.where(Conversation.assigned_user_id == assigned_user_id)
    if q:
        body_match = exists(
            select(Message.id).where(
                Message.org_id == org_id,
                Message.conversation_id == Conversation.id,
                func.to_tsvector("english", func.coalesce(Message.body_text, "")).op("@@")(
                    func.plainto_tsquery("english", q)
                ),
            )
        )
        stmt = stmt.where(or_(body_match, ChannelIdentity.display_name.ilike(f"%{q}%")))
    if cursor is not None:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(_cursor_predicate(sort_desc=sort_desc, ts=cursor_ts, id_=cursor_id))

    ts_col = Conversation.last_message_at
    order_col = ts_col.desc() if sort_desc else ts_col.asc()
    order_id = Conversation.id.desc() if sort_desc else Conversation.id.asc()
    # nullslast(): last_message_at is nullable, and a conversation with no
    # messages yet must not outrank live ones just because NULL sorts high
    # in Postgres' default DESC ordering.
    stmt = stmt.order_by(order_col.nullslast(), order_id)

    page_size = _clamp(limit, DEFAULT_CONVERSATION_LIMIT)
    stmt = stmt.limit(page_size + 1)

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    next_cursor = None
    if has_more and rows:
        last_conversation, _ = rows[-1]
        next_cursor = _encode_cursor(last_conversation.last_message_at, last_conversation.id)

    return ConversationPage(
        items=[_summary(conversation, identity) for conversation, identity in rows],
        next_cursor=next_cursor,
    )


async def get_conversation(
    session: AsyncSession,
    org_id: str,
    conversation_id: str,
    user_id: str,
    *,
    limit: int = DEFAULT_MESSAGE_LIMIT,
    before: str | None = None,
) -> ConversationDetail:
    """Read one conversation's thread -- the most recent ``limit`` messages (§3).

    Messages come back oldest-first (reading order), but are *selected* newest
    -first and reversed: a thread's tail is what an agent opens to, and taking
    the head would show the oldest messages of a long thread.

    Args:
        org_id: Org that must own the conversation.
        conversation_id: Thread to read.
        user_id: Caller; must be a member of ``org_id`` (any role, §4).
        limit: Max messages, clamped to ``MAX_LIMIT``.
        before: Opaque cursor from a previous call's ``messages_next_cursor``
            -- fetches the page of messages immediately older than it, for
            scrolling back through a long thread. Omit for the newest page.

    Raises:
        NotFoundError: Caller isn't a member of ``org_id``.
        ConversationNotFoundError: No such conversation *for this org*.
        InvalidQueryError: ``before`` doesn't decode to a valid cursor.

    Performance: < 200ms -- indexed thread read plus one batched attachment
    query and locally-signed URLs.
    """
    await access.require_membership(user_id, org_id)

    conversation = await access.load_conversation(session, org_id, conversation_id)

    identity = await session.get(ChannelIdentity, conversation.customer_identity_id)
    if identity is None:
        raise ConversationNotFoundError(
            f"Conversation {conversation_id!r} has no customer identity"
        )

    page_size = _clamp(limit, DEFAULT_MESSAGE_LIMIT)
    message_stmt = select(Message).where(
        Message.org_id == org_id, Message.conversation_id == conversation_id
    )
    if before is not None:
        before_ts, before_id = _decode_cursor(before)
        message_stmt = message_stmt.where(
            or_(
                Message.created_at < before_ts,
                and_(Message.created_at == before_ts, Message.id < before_id),
            )
        )
    message_stmt = message_stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(
        page_size + 1
    )

    fetched = list((await session.execute(message_stmt)).scalars().all())
    has_more = len(fetched) > page_size
    newest_first = fetched[:page_size]
    messages_next_cursor = (
        _encode_cursor(newest_first[-1].created_at, newest_first[-1].id)
        if has_more and newest_first
        else None
    )
    messages = list(reversed(newest_first))

    attachments = await _attachments_for(session, org_id, [m.id for m in messages])
    return ConversationDetail(
        conversation=_summary(conversation, identity),
        messages=[_message_view(m, attachments.get(m.id, [])) for m in messages],
        messages_next_cursor=messages_next_cursor,
    )


async def _attachments_for(
    session: AsyncSession, org_id: str, message_ids: list[str]
) -> dict[str, list[AttachmentView]]:
    """Load every message's attachments in one query, then sign each URL.

    One ``IN`` query rather than a per-message lookup -- a 50-message thread
    would otherwise be 50 round-trips. Signing itself is local (and Redis-cached
    by ``media``), so it stays off the database's critical path.
    """
    if not message_ids:
        return {}

    stmt = select(MessageAttachment).where(
        MessageAttachment.org_id == org_id,
        MessageAttachment.message_id.in_(message_ids),
    )
    rows = (await session.execute(stmt)).scalars().all()

    grouped: dict[str, list[AttachmentView]] = {}
    for row in rows:
        url = await media.signed_url_for_attachment(org_id, row.s3_key)
        grouped.setdefault(row.message_id, []).append(
            AttachmentView(
                id=row.id,
                filename=_filename_from_key(row.s3_key),
                content_type=row.content_type,
                size_bytes=row.size_bytes,
                url=url,
            )
        )
    return grouped


async def mark_read(
    session: AsyncSession, org_id: str, conversation_id: str, user_id: str
) -> Conversation:
    """Zero a conversation's unread counter.

    The counterpart to the worker's ``unread_count += 1``: without this the
    column only ever grows, so it could never mean anything. Kept as its own
    explicit call rather than a side effect of :func:`get_conversation` -- a
    GET that mutates would make prefetching or a double-render silently clear
    someone's unread badge.

    Raises:
        NotFoundError: Caller isn't a member of ``org_id``.
        ConversationNotFoundError: No such conversation for this org.
    """
    await access.require_membership(user_id, org_id)

    conversation = await access.load_conversation(session, org_id, conversation_id)

    conversation.unread_count = 0
    await session.commit()
    return conversation
