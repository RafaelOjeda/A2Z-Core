"""Fixtures for Omni-Channel Postgres integration tests.

Uses the real ``DATABASE_URL`` (the docker-compose ``postgres`` service
locally / in CI) -- unlike AWS services, there is no in-process Postgres
emulator equivalent to moto, so these tests require a reachable server.

Tables are created via ``Base.metadata.create_all()`` for test speed. The
Alembic migration itself (including the hand-added full-text GIN index,
which ``create_all()`` cannot produce since it isn't modeled as an ORM
column) was verified separately by actually running upgrade/downgrade
against a real database -- see docs/omnichannel-decisions.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.omnichannel import db, queues
from app.services.omnichannel.models import Base

pytestmark = pytest.mark.integration

# Whether this session has already rebuilt the schema from models.py.
_schema_rebuilt = False


@pytest.fixture(autouse=True)
def _fresh_queue_url_cache() -> Iterator[None]:
    """Reset the cached SQS queue URLs every test.

    ``queues._queue_url_cache`` is a plain module-level dict, not scoped to
    the ``aws`` (moto) fixture's per-test mock context -- a URL cached while
    one test's ``mock_aws()`` was active can otherwise leak into the next
    test's, whose backend state has already been reset (same failure shape
    as the lru_cache'd engine issue ``_fresh_engine`` below works around).
    """
    queues.reset_queue_url_cache()
    yield
    queues.reset_queue_url_cache()


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """Rebuild the engine every test, and the schema once per session.

    ``db.engine()``/``db.session_factory()`` are ``lru_cache``'d singletons
    (matching ``core.clients``), but pytest-asyncio hands each test function
    its own event loop by default -- a connection pool built in one test's
    loop breaks in the next. Core's own tests sidestep the same issue for
    ``clients.redis_client()`` by monkeypatching a fresh fake per test rather
    than reusing the cached singleton across tests; this does the
    equivalent for the real Postgres engine.

    The schema is dropped once per session, before the first
    ``create_all()``: create_all silently skips tables that already exist --
    **indexes included** -- so a database left over from an earlier run (or
    from a manual ``alembic upgrade``) keeps serving its old schema and masks
    any model change. Not hypothetical: it made an index-usage test pass
    against a stale index while models.py said otherwise. CI gets a fresh
    container per run and would never have caught it; local runs wouldn't
    either, without this.
    """
    global _schema_rebuilt
    db.reset_engine()
    engine = db.engine()
    async with engine.begin() as conn:
        if not _schema_rebuilt:
            await conn.execute(text("DROP SCHEMA IF EXISTS omnichannel CASCADE"))
            _schema_rebuilt = True
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS omnichannel"))
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        table_names = ", ".join(f"omnichannel.{t.name}" for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    await engine.dispose()
    db.reset_engine()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with db.session_factory()() as s:
        yield s
