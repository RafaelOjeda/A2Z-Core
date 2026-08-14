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

import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clients
from app.core.membership import Membership, Role
from app.services.omnichannel import db, queues, worker
from app.services.omnichannel.models import (
    Base,
    ChannelConnection,
    ChannelIdentity,
    Conversation,
)

pytestmark = pytest.mark.integration

# The Meta app secret every seeded connection's webhook signature is HMAC'd
# with -- shared across WhatsApp/Messenger/Instagram tests since they all go
# through the same MetaGraphAdapter.verify_inbound_signature.
APP_SECRET = "wa-app-secret"

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
    loop breaks in the next. Core's own tests sidestep the same issue for its
    boto3 client singletons via ``clients.reset_clients()``; this does the
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


# --- Shared helpers, reused across test_message_flow.py, test_message_flow_meta.py,
# and test_delivery_status_flow.py -- one signing/seeding implementation per
# concept rather than one copy per file. ---


def sign(raw_body: bytes, secret: str = APP_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


async def seed_secret(org_id: str, key: str, value: dict[str, str]) -> None:
    await clients.run_aws(
        clients.secretsmanager().create_secret,
        Name=f"a2z/{org_id}/omnichannel/{key}",
        SecretString=json.dumps(value),
    )


async def seed_connection(
    session: AsyncSession, org_id: str = "org-a", channel_type: str = "whatsapp"
) -> ChannelConnection:
    connection = ChannelConnection(
        org_id=org_id,
        channel_type=channel_type,
        display_name=f"Test {channel_type}",
        provider_account_id="15550001111",
        credentials_secret_key=f"{channel_type}-main",
        status="active",
    )
    session.add(connection)
    await session.commit()
    return connection


async def seed_identity_and_conversation(
    session: AsyncSession, org_id: str, channel_type: str = "whatsapp"
) -> Conversation:
    identity = ChannelIdentity(org_id=org_id, channel_type=channel_type, external_id="15551234567")
    session.add(identity)
    await session.flush()
    conversation = Conversation(org_id=org_id, customer_identity_id=identity.id, status="open")
    session.add(conversation)
    await session.commit()
    return conversation


def mock_realtime_and_events(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    mock_publish_event = AsyncMock()
    mock_publish_update = AsyncMock()
    monkeypatch.setattr(worker, "publish_event", mock_publish_event)
    monkeypatch.setattr(worker, "publish_update", mock_publish_update)
    return mock_publish_event, mock_publish_update


def member_stub(org_id: str, user_id: str = "u1") -> Membership:
    return Membership(
        user_id=user_id, org_id=org_id, role=Role.MEMBER, joined_at=datetime.now(timezone.utc)
    )


def mock_membership(monkeypatch: pytest.MonkeyPatch, org_id: str, user_id: str = "u1") -> None:
    monkeypatch.setattr(
        "app.services.omnichannel.access.get_membership",
        AsyncMock(return_value=member_stub(org_id, user_id)),
    )


def whatsapp_message_payload(
    from_number: str = "15551234567", text: str = "Hi there", wamid: str = "wamid.ABC"
) -> dict[str, Any]:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": from_number, "profile": {"name": "Jane"}}],
                            "messages": [
                                {
                                    "from": from_number,
                                    "id": wamid,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


def messenger_message_payload(
    sender_id: str = "USER1", text: str = "Hi there", mid: str = "m.ABC", page_id: str = "PAGE"
) -> dict[str, Any]:
    return {
        "entry": [
            {
                "id": page_id,
                "messaging": [
                    {
                        "sender": {"id": sender_id},
                        "recipient": {"id": page_id},
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ]
    }
