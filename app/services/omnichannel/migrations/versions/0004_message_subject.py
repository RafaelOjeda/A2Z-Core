"""Add messages.subject for outbound email replies.

Wires up plumbing that already existed but was dead: ``OutboundContent``
(``adapters/types.py``) has carried a ``subject`` field since v1, and
``EmailAdapter.send_outbound`` has always forwarded it to
``core.email.send_email`` -- but nothing upstream ever populated it, because
``Message`` had nowhere to persist a subject an agent typed. This migration
plus the accompanying ``SendReplyRequest.subject`` (routers/omnichannel.py)
close that gap for email; other channels simply ignore the column value.

No existing column can honestly hold this -- ``body_text`` is the message
body and ``content_type`` is a MIME type, not free text. Nullable, no
backfill, no index: every pre-existing row and every non-email send leaves
it ``NULL``.

Rollback: drops the column. Safe -- nothing has ever been stamped against a
real deployed database (known-issues.md #3), and no other column or index
depends on this one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_message_subject"
down_revision: str | None = "0003_message_dedup_key"
branch_labels: None = None
depends_on: None = None

_TABLE = "messages"
_SCHEMA = "omnichannel"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("subject", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "subject", schema=_SCHEMA)
