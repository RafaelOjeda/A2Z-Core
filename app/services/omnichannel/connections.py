"""Channel connection lookups — resolving which org owns a provider account.

Webhook payloads from Meta/AWS never carry ``org_id`` directly; the org's
own provider account id in the payload (a WhatsApp ``phone_number_id``, an
SMS origination number, a verified sending/receiving address) is the only
thing that ties an inbound webhook back to a specific org's
``channel_connections`` row.
"""

from __future__ import annotations

from sqlalchemy import select

from app.services.omnichannel import db
from app.services.omnichannel.models import ChannelConnection, ChannelType


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
