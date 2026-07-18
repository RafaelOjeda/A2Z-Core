"""Tests for channel-connections CRUD, and resolving an org from a channel's
provider account id (§5.6 — webhook payloads never carry org_id directly)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clients, secrets
from app.core.exceptions import NotFoundError, SecretNotFoundError
from app.core.membership import Membership, Role
from app.core.settings import get_org_settings
from app.services.omnichannel import connections, db, webhooks
from app.services.omnichannel.connections import resolve_org_by_provider_account
from app.services.omnichannel.exceptions import (
    ConnectionNotFoundError,
    ConnectionValidationError,
    ForbiddenError,
)
from app.services.omnichannel.models import ChannelConnection, ChannelType

pytestmark = pytest.mark.integration


def _membership(role: Role, org_id: str = "org-a") -> Membership:
    return Membership(
        user_id="admin-1", org_id=org_id, role=role, joined_at=datetime.now(timezone.utc)
    )


def _stub_membership(
    monkeypatch: pytest.MonkeyPatch, role: Role | None, org_id: str = "org-a"
) -> None:
    value = None if role is None else _membership(role, org_id)
    monkeypatch.setattr(connections, "get_membership", AsyncMock(return_value=value))


async def _seed_secret(org_id: str, key: str, value: dict[str, str]) -> None:
    await clients.run_aws(
        clients.secretsmanager().create_secret,
        Name=f"a2z/{org_id}/omnichannel/{key}",
        SecretString=json.dumps(value),
    )


# --- create_connection ---


async def test_create_connection_succeeds_for_admin(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    await _seed_secret("org-a", "whatsapp-main", {"app_secret": "x"})

    connection, dns_records = await connections.create_connection(
        session,
        "org-a",
        "admin-1",
        channel_type="whatsapp",
        display_name="Acme WhatsApp",
        provider_account_id="pn-123",
        credentials_secret_key="whatsapp-main",
    )

    assert connection.status == "active"
    assert connection.org_id == "org-a"
    assert dns_records is None


async def test_create_connection_self_service_credentials(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SaaS self-service path: no engineer pre-provisions a secret --
    the org's own submitted credentials are stored by create_connection."""
    _stub_membership(monkeypatch, Role.ADMIN)

    connection, _ = await connections.create_connection(
        session,
        "org-a",
        "admin-1",
        channel_type="whatsapp",
        display_name="Acme WhatsApp",
        provider_account_id="pn-123",
        credentials={"access_token": "tok", "phone_number_id": "pn-123", "app_secret": "wa-secret"},
    )

    assert connection.credentials_secret_key == connection.id
    stored = await secrets.get_secret("org-a", "omnichannel", connection.id)
    assert stored == {"access_token": "tok", "phone_number_id": "pn-123", "app_secret": "wa-secret"}


async def test_create_connection_rejects_both_credentials_forms(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    await _seed_secret("org-a", "whatsapp-main", {"app_secret": "x"})

    with pytest.raises(ConnectionValidationError):
        await connections.create_connection(
            session,
            "org-a",
            "admin-1",
            channel_type="whatsapp",
            display_name="Acme WhatsApp",
            provider_account_id="pn-123",
            credentials={"access_token": "tok"},
            credentials_secret_key="whatsapp-main",
        )


async def test_create_connection_rejects_no_credentials_form(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)

    with pytest.raises(ConnectionValidationError):
        await connections.create_connection(
            session,
            "org-a",
            "admin-1",
            channel_type="whatsapp",
            display_name="Acme WhatsApp",
            provider_account_id="pn-123",
        )


async def test_create_email_connection_needs_no_credentials(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Email authenticates via the org's verified sending domain, not a secret."""
    _stub_membership(monkeypatch, Role.ADMIN)

    connection, dns_records = await connections.create_connection(
        session,
        "org-a",
        "admin-1",
        channel_type="email",
        display_name="Support inbox",
        provider_account_id="support@acme.com",
    )

    assert connection.credentials_secret_key == ""
    assert dns_records is not None
    assert dns_records.domain == "acme.com"
    assert dns_records.verification_txt_name == "_amazonses.acme.com"
    assert len(dns_records.dkim_cname_records) == 3

    org = await get_org_settings("org-a")
    assert org.domain == "acme.com"


async def test_create_email_connection_requires_address_form(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)

    with pytest.raises(ConnectionValidationError):
        await connections.create_connection(
            session,
            "org-a",
            "admin-1",
            channel_type="email",
            display_name="Support inbox",
            provider_account_id="not-an-address",
        )


async def test_create_connection_requires_admin_role(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.MEMBER)
    await _seed_secret("org-a", "whatsapp-main", {"app_secret": "x"})

    with pytest.raises(ForbiddenError):
        await connections.create_connection(
            session,
            "org-a",
            "admin-1",
            channel_type="whatsapp",
            display_name="Acme WhatsApp",
            provider_account_id="pn-123",
            credentials_secret_key="whatsapp-main",
        )


async def test_create_connection_requires_membership(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, None)

    with pytest.raises(NotFoundError):
        await connections.create_connection(
            session,
            "org-a",
            "stranger",
            channel_type="whatsapp",
            display_name="x",
            provider_account_id="pn-123",
            credentials_secret_key="whatsapp-main",
        )


async def test_create_connection_rejects_unregistered_channel_type(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMS has a full adapter (known-issues.md #1) but is deliberately unregistered."""
    _stub_membership(monkeypatch, Role.ADMIN)

    with pytest.raises(ConnectionValidationError):
        await connections.create_connection(
            session,
            "org-a",
            "admin-1",
            channel_type="sms",
            display_name="Acme SMS",
            provider_account_id="+15550001111",
            credentials_secret_key="sms-main",
        )


async def test_create_connection_requires_existing_secret(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)

    with pytest.raises(SecretNotFoundError):
        await connections.create_connection(
            session,
            "org-a",
            "admin-1",
            channel_type="whatsapp",
            display_name="Acme WhatsApp",
            provider_account_id="pn-123",
            credentials_secret_key="does-not-exist",
        )


# --- list_connections / get_connection ---


async def test_list_connections_scoped_to_org(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN, org_id="org-a")
    await _seed_secret("org-a", "k1", {"app_secret": "x"})
    await connections.create_connection(
        session,
        "org-a",
        "admin-1",
        channel_type="whatsapp",
        display_name="A",
        provider_account_id="pn-1",
        credentials_secret_key="k1",
    )
    session.add(
        ChannelConnection(
            org_id="org-b",
            channel_type="whatsapp",
            display_name="B",
            provider_account_id="pn-2",
            credentials_secret_key="k2",
        )
    )
    await session.commit()

    result = await connections.list_connections(session, "org-a", "admin-1")

    assert [c.org_id for c in result] == ["org-a"]


async def test_get_connection_cross_org_is_not_found(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = ChannelConnection(
        org_id="org-b",
        channel_type="whatsapp",
        display_name="B",
        provider_account_id="pn-2",
        credentials_secret_key="k2",
    )
    session.add(other)
    await session.commit()

    _stub_membership(monkeypatch, Role.ADMIN, org_id="org-a")
    with pytest.raises(ConnectionNotFoundError):
        await connections.get_connection(session, "org-a", "admin-1", other.id)


# --- disable_connection ---


async def test_disable_connection_sets_status(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    await _seed_secret("org-a", "k1", {"app_secret": "x"})
    connection, _ = await connections.create_connection(
        session,
        "org-a",
        "admin-1",
        channel_type="whatsapp",
        display_name="A",
        provider_account_id="pn-1",
        credentials_secret_key="k1",
    )

    disabled = await connections.disable_connection(session, "org-a", "admin-1", connection.id)

    assert disabled.status == "disabled"


async def test_disabled_connection_rejects_inbound_webhooks(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    await _seed_secret("org-a", "k1", {"app_secret": "wa-app-secret"})
    connection, _ = await connections.create_connection(
        session,
        "org-a",
        "admin-1",
        channel_type="whatsapp",
        display_name="A",
        provider_account_id="pn-1",
        credentials_secret_key="k1",
    )
    await connections.disable_connection(session, "org-a", "admin-1", connection.id)

    with pytest.raises(ConnectionNotFoundError):
        await webhooks.handle_webhook(session, "whatsapp", connection.id, b"{}", {})


async def test_resolves_known_provider_account() -> None:
    async with db.get_session_context() as session:
        session.add(
            ChannelConnection(
                org_id="org-a",
                channel_type=ChannelType.WHATSAPP.value,
                display_name="Acme WhatsApp",
                provider_account_id="pn-123",
                credentials_secret_key="whatsapp_token",
            )
        )
        await session.commit()

    org_id = await resolve_org_by_provider_account(ChannelType.WHATSAPP, "pn-123")
    assert org_id == "org-a"


async def test_returns_none_for_unknown_provider_account() -> None:
    org_id = await resolve_org_by_provider_account(ChannelType.WHATSAPP, "does-not-exist")
    assert org_id is None


async def test_scoped_by_channel_type_not_just_provider_account_id() -> None:
    async with db.get_session_context() as session:
        session.add(
            ChannelConnection(
                org_id="org-a",
                channel_type=ChannelType.SMS.value,
                display_name="Acme SMS",
                provider_account_id="shared-id",
                credentials_secret_key="sms_key",
            )
        )
        await session.commit()

    # Same provider_account_id string, different channel_type -> no match.
    assert await resolve_org_by_provider_account(ChannelType.WHATSAPP, "shared-id") is None
    assert await resolve_org_by_provider_account(ChannelType.SMS, "shared-id") == "org-a"
