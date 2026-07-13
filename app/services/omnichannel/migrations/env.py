"""Alembic environment for Omni-Channel's ``omnichannel`` Postgres schema.

The database URL always comes from ``app.config.settings()`` (``DATABASE_URL``)
-- never duplicated into ``alembic.ini`` -- so there is exactly one place
that knows how to reach Postgres, matching the ``core.clients`` singleton
pattern used for every other backing store.

The ``omnichannel`` schema (and Alembic's own version-tracking table inside
it) is created explicitly before migrations run, since Alembic can't create
tables in a schema that doesn't exist yet.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import settings
from app.services.omnichannel.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
_SCHEMA = "omnichannel"


def run_migrations_offline() -> None:
    context.configure(
        url=settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=_SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    # Committed as its own transaction: otherwise this execute() auto-begins
    # a transaction that Alembic's begin_transaction() below treats as
    # caller-owned and won't commit itself, so it gets silently rolled back
    # when the connection closes (SQLAlchemy 2.0 closes uncommitted
    # transactions with a rollback, not a commit).
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}"))
    connection.commit()

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=_SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable: AsyncEngine = create_async_engine(settings().database_url)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
