"""Tests for resolving an org from a channel's provider account id (§5.6 —
webhook payloads never carry org_id directly)."""

from __future__ import annotations

import pytest

from app.services.omnichannel import db
from app.services.omnichannel.connections import resolve_org_by_provider_account
from app.services.omnichannel.models import ChannelConnection, ChannelType

pytestmark = pytest.mark.integration


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
