"""Add messages.client_dedup_key + a partial unique index for send idempotency.

Added for the ``Idempotency-Key`` header on ``POST .../messages``
(API review, 2026-07-18, "no idempotency support on message send" finding):
a retried request after a dropped response previously created a second
outbound message and sent it twice. The client-supplied key is stored per
``(org_id, conversation_id)`` -- distinct client keys never collide across
conversations or orgs, matching every other uniqueness rule in this schema
(root CLAUDE.md golden rule #2).

Nullable column + a *partial* unique index (``WHERE client_dedup_key IS NOT
NULL``) rather than widening the existing ``uq_message_idempotency``
constraint: a caller that omits the header (the common case, and every row
written before this migration) must not be forced into a fabricated unique
value just to satisfy the index.

Additive, nullable column + a new index only -- no existing data rewritten,
downgrade drops both cleanly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_message_dedup_key"
down_revision: str | None = "0002_inbox_index"
branch_labels: None = None
depends_on: None = None

_INDEX = "uq_message_client_dedup"
_TABLE = "messages"
_SCHEMA = "omnichannel"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("client_dedup_key", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.execute(
        f"CREATE UNIQUE INDEX {_INDEX} ON {_SCHEMA}.{_TABLE} "
        "(org_id, conversation_id, client_dedup_key) "
        "WHERE client_dedup_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_SCHEMA}.{_INDEX}")
    op.drop_column(_TABLE, "client_dedup_key", schema=_SCHEMA)
