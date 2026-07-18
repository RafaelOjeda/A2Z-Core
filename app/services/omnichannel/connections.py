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
(``routing.py::set_routing_config``).

**Self-service credentials (2026-07-18 addition):** this is a SaaS product --
an org admin connecting their own WhatsApp number can't be gated on an
engineer hand-writing a Secrets Manager entry. For a channel whose adapter
``requires_credentials`` (§7 ``SupportedFeatures``), the caller now passes
``credentials`` (the raw token/secret dict the org just typed into a form)
and this module writes it via ``core.secrets.put_secret`` under a key
derived from the new connection's own id -- never re-using a caller-supplied
key, so two connections can never collide or overwrite each other's secret.
The old path -- passing a pre-existing ``credentials_secret_key`` that an
engineer provisioned out of band -- still works (useful for a shared/rotated
credential), but exactly one of the two must be given, never both.

Email needs no secret at all (``requires_credentials=False`` -- it
authenticates via the org's verified SES sending domain, not a per-connection
token). Creating an email connection instead kicks off SES domain
verification (``core.email.start_domain_verification``) for the domain in
``provider_account_id`` and hands back the DNS records the org must add --
the email equivalent of "no manual AWS step."
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import email as core_email
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
    # Populated only by create_connection's response for a brand-new email
    # connection -- the DNS records the org still needs to add. Re-fetch via
    # core.email.get_domain_verification_status for anything already created.
    dns_records: core_email.DomainVerificationRecords | None = None


class ConnectionPage(BaseModel):
    items: list[ConnectionView]


def to_view(
    connection: ChannelConnection,
    dns_records: core_email.DomainVerificationRecords | None = None,
) -> ConnectionView:
    return ConnectionView(
        id=connection.id,
        channel_type=connection.channel_type,
        display_name=connection.display_name,
        provider_account_id=connection.provider_account_id,
        credentials_secret_key=connection.credentials_secret_key,
        status=connection.status,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
        dns_records=dns_records,
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
    credentials: dict[str, Any] | None = None,
    credentials_secret_key: str | None = None,
) -> tuple[ChannelConnection, core_email.DomainVerificationRecords | None]:
    """Register a new channel connection for an org (Owner/Admin only).

    Args:
        channel_type: Must be a channel with a registered adapter (§5.2) --
            e.g. SMS's adapter exists but is unregistered (known-issues.md
            #1), so ``channel_type="sms"`` is rejected here too.
        credentials: Raw credential values a user just submitted (e.g. a
            WhatsApp ``access_token``/``phone_number_id``/``app_secret``) --
            the self-service path. Written to ``core.secrets`` under a key
            derived from this connection's own id, so two connections can
            never collide on or overwrite each other's secret.
        credentials_secret_key: A ``core.secrets`` key an engineer already
            provisioned out of band. Mutually exclusive with ``credentials``.
            For a channel whose adapter ``requires_credentials``, exactly one
            of the two must be given. Ignored entirely for a channel that
            doesn't need credentials (email -- it authenticates via the
            org's verified sending domain instead, see below).

    Returns:
        The new connection, and -- for ``channel_type="email"`` only -- the
        DNS records the org must add to finish verifying
        ``provider_account_id``'s domain (``None`` for every other channel).
        Domain verification is kicked off here, not as a separate step:
        ``provider_account_id`` must be an ``address@domain`` string.

    Raises:
        NotFoundError: Actor isn't a member of ``org_id``.
        ForbiddenError: Actor's role isn't Owner/Admin.
        ConnectionValidationError: ``channel_type`` has no registered
            adapter; both or neither of ``credentials``/``credentials_secret_key``
            were given for a credential-requiring channel; or (email only)
            ``provider_account_id`` isn't an ``address@domain`` string.
        SecretNotFoundError: ``credentials_secret_key`` doesn't resolve.
        InvalidAddressError: (email only) the derived domain is implausible.

    Performance: < 300ms (one secret write/lookup, or two SES calls for
    email verification, plus one insert and one audit write).
    """
    await _require_admin(org_id, actor_user_id)

    try:
        adapter = get_adapter(channel_type)
    except ChannelAdapterError as exc:
        raise ConnectionValidationError(str(exc)) from exc

    if credentials is not None and credentials_secret_key is not None:
        raise ConnectionValidationError(
            "Pass either credentials or credentials_secret_key, not both"
        )

    connection_id = str(uuid.uuid4())
    dns_records: core_email.DomainVerificationRecords | None = None

    if adapter.supported_features.requires_credentials:
        if credentials is not None:
            secret_key = connection_id
            await secrets.put_secret(org_id, _SERVICE_TYPE, secret_key, credentials)
        elif credentials_secret_key is not None:
            secret_key = credentials_secret_key
            await secrets.get_secret(org_id, _SERVICE_TYPE, secret_key)
        else:
            raise ConnectionValidationError(
                f"channel_type={channel_type!r} requires credentials "
                "(pass credentials or credentials_secret_key)"
            )
    else:
        if "@" not in provider_account_id:
            raise ConnectionValidationError(
                "email connections require provider_account_id to be an address (user@domain)"
            )
        secret_key = ""
        domain = provider_account_id.rsplit("@", 1)[1]
        dns_records = await core_email.start_domain_verification(org_id, domain, actor_user_id)

    connection = ChannelConnection(
        id=connection_id,
        org_id=org_id,
        channel_type=channel_type,
        display_name=display_name,
        provider_account_id=provider_account_id,
        credentials_secret_key=secret_key,
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
    return connection, dns_records


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
