"""Rebuild ix_conversations_inbox as DESC NULLS LAST so the inbox query uses it.

The Step 2 baseline created this index ascending:

    (org_id, status, last_message_at)

but the inbox query (``inbox.list_conversations``, Build Order Step 9) reads:

    ... WHERE org_id = ? [AND status = ?]
    ORDER BY last_message_at DESC NULLS LAST

A btree can only satisfy an ORDER BY whose direction *and* null placement it
matches (forward or backward). Ascending matches neither, so the planner used
the index to filter and then added a Sort on top -- fine at test scale, a
sort of every open conversation in the org at real scale.

Notably §5.1 specified ``last_message_at DESC``, which the baseline didn't
implement -- but plain DESC would *also* still sort here, because Postgres
defaults DESC to NULLS FIRST while the query wants NULLS LAST. Confirmed with
EXPLAIN against 20k rows:

    (org_id, status, last_message_at)                  -> Sort
    (org_id, status, last_message_at DESC)             -> Sort
    (org_id, status, last_message_at DESC NULLS LAST)  -> no Sort

``last_message_at`` is nullable (a conversation is flushed before the worker
stamps it), so the null placement is not hypothetical: NULLS LAST is what
keeps a never-active conversation from outranking live ones.

Index-only change: no data is rewritten and downgrade restores the previous
definition exactly, so this is safe to run and roll back on a live table.
"""

from __future__ import annotations

from alembic import op

revision: str = "0002_inbox_index"
down_revision: str | None = "1bfacee578a4"
branch_labels: None = None
depends_on: None = None

_INDEX = "ix_conversations_inbox"
_TABLE = "omnichannel.conversations"


def upgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS omnichannel.{_INDEX}")
    op.execute(
        f"CREATE INDEX {_INDEX} ON {_TABLE} (org_id, status, last_message_at DESC NULLS LAST)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS omnichannel.{_INDEX}")
    op.execute(f"CREATE INDEX {_INDEX} ON {_TABLE} (org_id, status, last_message_at)")
