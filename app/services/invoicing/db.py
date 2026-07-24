"""Async SQLAlchemy engine/session for Invoicing (app/services/invoicing/CLAUDE.md §7).

Same shared Postgres instance as Omni-Channel, different schema
(``invoicing`` vs. ``omnichannel``) -- one ``DATABASE_URL``, one engine
factory per service, mirroring ``app/services/omnichannel/db.py`` exactly.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


@lru_cache(maxsize=1)
def engine() -> AsyncEngine:
    """The shared async engine, built once (mirrors core.clients singletons)."""
    return create_async_engine(settings().database_url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session, always closed after the request."""
    async with session_factory()() as session:
        yield session


@asynccontextmanager
async def get_session_context() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager for direct use outside FastAPI routes."""
    async with session_factory()() as session:
        yield session


def reset_engine() -> None:
    """Clear the cached engine/session factory. Used by tests between runs."""
    engine.cache_clear()
    session_factory.cache_clear()
