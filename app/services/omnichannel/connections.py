"""Channel connections: org-scoped CRUD, plus the provider-account lookup
webhooks use to resolve org_id.

Webhook payloads from Meta/AWS never carry ``org_id`` directly; the org's
own provider account id in the payload (a WhatsApp ``phone_number_id``, an
SMS origination number, a verified sending/receiving address) is the only
thing that ties an inbound webhook back to a specific org's
``channel_connections`` row (``resolve_org_by_provider_account``).

The CRUD functions below (API review, 2026-07-18) fill a gap the original
build left open: nothing in the API surface could create a
``channel_connections`` row, so webhooks and outbound sends had no way to
exist for an org except a manual DB insert. Owner/Admin only -- a
connection is infra configuration, same authz tier as routing config
(``routing.py::set_routing_config``). Credentials are never accepted or
returned here: ``credentials_secret_key`` is a *reference* into
``core.secrets`` (the ``a2z/{org_id}/omnichannel/{key}`` convention already
on the model), verified to exist at create time; provisioning the secret's
actual value stays an ops step, matching Core's frozen, read-only
``core.secrets.get_secret`` API (no ``put_secret`` exists to call).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secrets
from app.core.audit import log_audit
from app.core.events import publish_event
from app.core.exceptions import NotFoundError
from app.core.membership import Role, get_membership
from app.services.omnichannel import db
from app.services.omnichannel.adapters.registry import get_adapter
from app.services.omnichannel.exceptions import (
    ChannelAdapterError,
    ConnectionNotFoundError,
    ConnectionValidationError,
    ForbiddenError,
)
from app.services.omnichannel.models import ChannelConnection, ChannelType

_SERVICE_TYPE = "omnichannel"


class ConnectionView(BaseModel):
    id: str
    channel_type: str
    display_name: str
    provider_account_id: str
    credentials_secret_key: str
    status: str
    created_at: datetime
    updated_at: datetime


class ConnectionPage(BaseModel):
    items: list[ConnectionView]


def to_view(connection: ChannelConnection) -> ConnectionView:
    return ConnectionView(
        id=connection.id,
        channel_type=connection.channel_type,
        display_name=connection.display_name,
        provider_account_id=connection.provider_account_id,
        credentials_secret_key=connection.credentials_secret_key,
        status=connection.status,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


async def _require_admin(org_id: str, actor_user_id: str) -> None:
    membership = await get_membership(actor_user_id, org_id)
    if membership is None:
        raise NotFoundError("Not a member of this org")
    if membership.role not in (Role.OWNER, Role.ADMIN):
        raise ForbiddenError("Only Owner/Admin can manage channel connections")


async def _load(session: AsyncSession, org_id: str, connection_id: str) -> ChannelConnection:
    connection = await session.get(ChannelConnection, connection_id)
    if connection is None or connection.org_id != org_id:
        # Same error either way -- whether it doesn't exist or belongs to
        # another org is itself information we don't hand out (same
        # convention as inbox.get_conversation).
        raise ConnectionNotFoundError(f"No channel connection {connection_id!r} for org {org_id!r}")
    return connection


async def create_connection(
    session: AsyncSession,
    org_id: str,
    actor_user_id: str,
    *,
    channel_type: str,
    display_name: str,
    provider_account_id: str,
    credentials_secret_key: str,
) -> ChannelConnection:
    """Register a new channel connection for an org (Owner/Admin only).

    Args:
        channel_type: Must be a channel with a registered adapter (§5.2) --
            e.g. SMS's adapter exists but is unregistered (known-issues.md
            #1), so ``channel_type="sms"`` is rejected here too.
        credentials_secret_key: Must already exist at
            ``a2z/{org_id}/omnichannel/{credentials_secret_key}`` in
            ``core.secrets`` -- this call verifies but never creates it.

    Raises:
        NotFoundError: Actor isn't a member of ``org_id``.
        ForbiddenError: Actor's role isn't Owner/Admin.
        ConnectionValidationError: ``channel_type`` has no registered adapter.
        SecretNotFoundError: ``credentials_secret_key`` doesn't resolve.

    Performance: < 200ms (one secret lookup, one insert, one audit write).
    """
    await _require_admin(org_id, actor_user_id)

    try:
        get_adapter(channel_type)
    except ChannelAdapterError as exc:
        raise ConnectionValidationError(str(exc)) from exc

    await secrets.get_secret(org_id, _SERVICE_TYPE, credentials_secret_key)

    connection = ChannelConnection(
        org_id=org_id,
        channel_type=channel_type,
        display_name=display_name,
        provider_account_id=provider_account_id,
        credentials_secret_key=credentials_secret_key,
        status="active",
    )
    session.add(connection)
    await session.commit()

    await log_audit(
        org_id,
        actor_user_id,
        "connection.created",
        "channel_connection",
        connection.id,
        {"channel_type": channel_type, "display_name": display_name},
    )
    await publish_event(
        org_id,
        "connection.created",
        {"connection_id": connection.id, "channel_type": channel_type},
        source="a2z.omnichannel",
    )
    return connection


async def list_connections(
    session: AsyncSession, org_id: str, actor_user_id: str
) -> list[ChannelConnection]:
    """List an org's channel connections (Owner/Admin only).

    Raises:
        NotFoundError: Actor isn't a member of ``org_id``.
        ForbiddenError: Actor's role isn't Owner/Admin.
    """
    await _require_admin(org_id, actor_user_id)
    stmt = (
        select(ChannelConnection)
        .where(ChannelConnection.org_id == org_id)
        .order_by(ChannelConnection.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_connection(
    session: AsyncSession, org_id: str, actor_user_id: str, connection_id: str
) -> ChannelConnection:
    """Read one channel connection (Owner/Admin only).

    Raises:
        NotFoundError: Actor isn't a member of ``org_id``.
        ForbiddenError: Actor's role isn't Owner/Admin.
        ConnectionNotFoundError: No such connection for this org.
    """
    await _require_admin(org_id, actor_user_id)
    return await _load(session, org_id, connection_id)


async def disable_connection(
    session: AsyncSession, org_id: str, actor_user_id: str, connection_id: str
) -> ChannelConnection:
    """Soft-disable a channel connection (Owner/Admin only).

    Sets ``status="disabled"`` rather than deleting the row -- assignment
    and message history reference connections indirectly (via
    ``channel_identities``/``messages.channel_type``), and a hard delete
    would orphan nothing usefully while making the connection's provider
    account id available for reuse mid-flight. A disabled connection's
    inbound webhooks are rejected (``webhooks.py::_load_connection``) as if
    the connection didn't exist.

    Raises:
        NotFoundError: Actor isn't a member of ``org_id``.
        ForbiddenError: Actor's role isn't Owner/Admin.
        ConnectionNotFoundError: No such connection for this org.
    """
    await _require_admin(org_id, actor_user_id)
    connection = await _load(session, org_id, connection_id)
    connection.status = "disabled"
    await session.commit()

    await log_audit(
        org_id,
        actor_user_id,
        "connection.disabled",
        "channel_connection",
        connection.id,
        {"channel_type": connection.channel_type},
    )
    await publish_event(
        org_id,
        "connection.disabled",
        {"connection_id": connection.id, "channel_type": connection.channel_type},
        source="a2z.omnichannel",
    )
    return connection


async def resolve_org_by_provider_account(
    channel_type: ChannelType, provider_account_id: str
) -> str | None:
    """Return the org_id owning this provider account, or None if unknown."""
    async with db.get_session_context() as session:
        result = await session.execute(
            select(ChannelConnection.org_id).where(
                ChannelConnection.channel_type == channel_type.value,
                ChannelConnection.provider_account_id == provider_account_id,
            )
        )
        return result.scalar_one_or_none()
