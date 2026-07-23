"""Fixtures for Invoicing Postgres integration tests.

Mirrors ``tests/integration/omnichannel/conftest.py`` exactly (own schema,
own ``lru_cache``'d engine) -- see that file's docstring for why the schema
is dropped once per session and the engine rebuilt every test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.invoicing import db
from app.services.invoicing.models import Base

pytestmark = pytest.mark.integration

_schema_rebuilt = False


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    global _schema_rebuilt
    db.reset_engine()
    engine = db.engine()
    async with engine.begin() as conn:
        if not _schema_rebuilt:
            await conn.execute(text("DROP SCHEMA IF EXISTS invoicing CASCADE"))
            _schema_rebuilt = True
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS invoicing"))
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        table_names = ", ".join(f"invoicing.{t.name}" for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    await engine.dispose()
    db.reset_engine()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with db.session_factory()() as s:
        yield s
