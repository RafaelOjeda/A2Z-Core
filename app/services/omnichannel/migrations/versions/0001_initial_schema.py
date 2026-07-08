"""Omni-Channel baseline schema

Creates the ``omnichannel`` schema and all tables from
app/services/omnichannel/CLAUDE.md §5.1: channel_connections,
channel_identities, conversations, messages, message_attachments,
conversation_assignments, presence, templates, commission_rules,
commission_attributions -- with every index and the
(channel_type, external_message_id) uniqueness guarantee on messages.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "omnichannel"


def upgrade() -> None:
    # The schema itself is created by migrations/env.py before this migration
    # runs (Alembic's own version table lives in it too); idempotent here
    # too so this migration stays reproducible in isolation.
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        "channel_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("channel_type", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("provider_account_id", sa.String(), nullable=False),
        sa.Column("credentials_secret_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_channel_connections_org_id", "channel_connections", ["org_id"], schema=SCHEMA
    )

    op.create_table(
        "channel_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("channel_type", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("display_name", sa.String()),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "channel_type", "external_id"),
        schema=SCHEMA,
    )
    op.create_index("ix_channel_identities_org_id", "channel_identities", ["org_id"], schema=SCHEMA)

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column(
            "customer_identity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.channel_identities.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("assigned_user_id", sa.String()),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("last_message_preview", sa.String()),
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_conversations_org_id", "conversations", ["org_id"], schema=SCHEMA)
    op.create_index(
        "ix_conversations_inbox",
        "conversations",
        ["org_id", "status", "last_message_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_conversations_agent_inbox",
        "conversations",
        ["org_id", "assigned_user_id", "status"],
        schema=SCHEMA,
    )

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.conversations.id"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("channel_type", sa.String(), nullable=False),
        sa.Column("external_message_id", sa.String(), nullable=False),
        sa.Column("body_text", sa.String()),
        sa.Column("content_type", sa.String(), nullable=False, server_default="text"),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("sent_by_user_id", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        # The webhook-idempotency guarantee -- do not remove.
        sa.UniqueConstraint("channel_type", "external_message_id"),
        schema=SCHEMA,
    )
    op.create_index("ix_messages_org_id", "messages", ["org_id"], schema=SCHEMA)
    op.create_index(
        "ix_messages_thread", "messages", ["conversation_id", "created_at"], schema=SCHEMA
    )
    op.execute(
        f"CREATE INDEX ix_messages_body_fts ON {SCHEMA}.messages "
        "USING gin (to_tsvector('english', body_text))"
    )

    op.create_table(
        "message_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.messages.id"),
            nullable=False,
        ),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("s3_key", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_message_attachments_message_id", "message_attachments", ["message_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_message_attachments_org_id", "message_attachments", ["org_id"], schema=SCHEMA
    )

    op.create_table(
        "conversation_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.conversations.id"),
            nullable=False,
        ),
        sa.Column("assigned_user_id", sa.String(), nullable=False),
        sa.Column("assigned_by", sa.String(), nullable=False),
        sa.Column("reason", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_conversation_assignments_org_id", "conversation_assignments", ["org_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_assignments_conversation",
        "conversation_assignments",
        ["conversation_id", "created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "presence",
        sa.Column("org_id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=SCHEMA,
    )

    op.create_table(
        "templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("channel_type", sa.String()),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("variables", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("provider_template_id", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "name"),
        schema=SCHEMA,
    )
    op.create_index("ix_templates_org_id", "templates", ["org_id"], schema=SCHEMA)

    op.create_table(
        "commission_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_commission_rules_org_id", "commission_rules", ["org_id"], schema=SCHEMA)
    op.create_index(
        "ix_commission_rules_current",
        "commission_rules",
        ["org_id", "effective_from"],
        schema=SCHEMA,
    )

    op.create_table(
        "commission_attributions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("invoice_id", sa.String(), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.conversations.id"),
            nullable=False,
        ),
        sa.Column("agent_user_id", sa.String(), nullable=False),
        sa.Column("rule_snapshot", sa.Numeric(5, 2), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2)),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_commission_attributions_org_id", "commission_attributions", ["org_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_attributions_agent_dashboard",
        "commission_attributions",
        ["org_id", "agent_user_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("commission_attributions", schema=SCHEMA)
    op.drop_table("commission_rules", schema=SCHEMA)
    op.drop_table("templates", schema=SCHEMA)
    op.drop_table("presence", schema=SCHEMA)
    op.drop_table("conversation_assignments", schema=SCHEMA)
    op.drop_table("message_attachments", schema=SCHEMA)
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.ix_messages_body_fts")
    op.drop_table("messages", schema=SCHEMA)
    op.drop_table("conversations", schema=SCHEMA)
    op.drop_table("channel_identities", schema=SCHEMA)
    op.drop_table("channel_connections", schema=SCHEMA)
