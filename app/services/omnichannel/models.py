"""SQLAlchemy models for Omni-Channel's Postgres data layer (CLAUDE.md §5.1).

Dedicated ``omnichannel`` schema on the shared Postgres instance (root
CLAUDE.md §2; app/services/omnichannel/CLAUDE.md §12) — never a second
instance. Every table carries ``org_id`` and every query must filter on it
(root golden rule #2); there is no cross-org read path.

``channel_type`` is ``TEXT`` everywhere, never a Postgres ``ENUM`` — adding a
channel must never require a schema migration (§5.1, §5.2 extensibility
invariants).

The full-text GIN index on ``messages.body_text`` is a functional index
(``to_tsvector(...)``), which SQLAlchemy's ORM layer has no first-class
column type for — it is created directly in the Alembic migration, not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy import UniqueConstraint as UQ
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(schema="omnichannel")


def _uuid() -> str:
    return str(uuid.uuid4())


class ChannelConnection(Base):
    """An org's live link to one channel account: credentials, status (§5.1)."""

    __tablename__ = "channel_connections"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    provider_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Points at a core.secrets key (a2z/{org_id}/{service_type}/{key}) -- never the secret itself.
    credentials_secret_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChannelIdentity(Base):
    """A customer's handle on a channel — a phone number, an email address (§5.1)."""

    __tablename__ = "channel_identities"
    __table_args__ = (UQ("org_id", "channel_type", "external_id", name="uq_channel_identity"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    # Nullable link for cross-channel merge -- agent-confirmed only (docs/omnichannel-decisions.md).
    customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    """The org-scoped thread with one customer (§5.1)."""

    __tablename__ = "conversations"
    __table_args__ = (
        # last_message_at is DESC NULLS LAST, not plain ASC: the inbox query
        # (inbox.list_conversations) orders by exactly this, and a btree only
        # serves an ORDER BY whose direction *and* null placement it matches.
        # A plain ASC index -- or even the `DESC` §5.1 originally called for,
        # since DESC defaults to NULLS FIRST -- leaves the planner adding a
        # Sort on top. Verified with EXPLAIN; see migration 0002.
        Index(
            "ix_conversations_inbox",
            "org_id",
            "status",
            text("last_message_at DESC NULLS LAST"),
        ),
        Index("ix_conversations_agent_inbox", "org_id", "assigned_user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    customer_identity_id: Mapped[str] = mapped_column(
        String, ForeignKey("omnichannel.channel_identities.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    assigned_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_preview: Mapped[str | None] = mapped_column(Text)
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    """One inbound/outbound item in a conversation (§5.1).

    ``(channel_type, external_message_id)`` is the webhook-idempotency
    guarantee — providers retry aggressively (§5.6); this unique constraint
    is load-bearing, not incidental.
    """

    __tablename__ = "messages"
    __table_args__ = (
        UQ("channel_type", "external_message_id", name="uq_message_idempotency"),
        Index("ix_messages_thread", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("omnichannel.conversations.id"), nullable=False
    )
    direction: Mapped[str] = mapped_column(Text, nullable=False)  # inbound | outbound
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, default="text/plain")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="received")
    sent_by_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MessageAttachment(Base):
    """Media attached to a message, stored via core.storage (§5.1)."""

    __tablename__ = "message_attachments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(
        String, ForeignKey("omnichannel.messages.id"), nullable=False, index=True
    )
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)  # {org_id}/omnichannel/...
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)


class ConversationAssignment(Base):
    """Append-only assignment history — never updated, never deleted (§5.1, §5.3).

    This is what makes commission replayable, and it's written from v1 day
    one even though auto-routing/commission themselves are deferred (§15).
    """

    __tablename__ = "conversation_assignments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("omnichannel.conversations.id"), nullable=False, index=True
    )
    assigned_user_id: Mapped[str] = mapped_column(String, nullable=False)
    assigned_by: Mapped[str] = mapped_column(Text, nullable=False)  # user id or "routing:*"
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Presence(Base):
    """Backup/audit row per agent — live state is Redis, not this table (§5.3).

    Unused in v1 (auto-routing/presence deferred, §15); table ships now so
    it costs nothing to have ready.
    """

    __tablename__ = "presence"
    __table_args__ = (UQ("org_id", "user_id", name="uq_presence_org_user"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="offline")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Template(Base):
    """A saved reply (§5.1). Unused in v1 (templates deferred, §15)."""

    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    channel_type: Mapped[str | None] = mapped_column(Text, nullable=True)  # null = any channel
    body: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    provider_template_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CommissionRule(Base):
    """Append-only history — current rule is the latest row (§5.5).

    Unused in v1 (commission deferred until Invoicing exists, §15); table
    ships now so commission is subscriber code only when it lands.
    """

    __tablename__ = "commission_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CommissionAttribution(Base):
    """Snapshot the assigned agent at invoice-creation time, not payment time (§5.5).

    Unused in v1 (deferred, §15) — ships now so the load-bearing snapshot
    rule has somewhere to land the moment ``invoice.paid`` exists.
    """

    __tablename__ = "commission_attributions"
    __table_args__ = (Index("ix_commission_attributions_dashboard", "org_id", "agent_user_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    invoice_id: Mapped[str] = mapped_column(String, nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("omnichannel.conversations.id"), nullable=False
    )
    agent_user_id: Mapped[str] = mapped_column(String, nullable=False)
    rule_snapshot: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
