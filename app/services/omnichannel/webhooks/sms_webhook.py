"""SMS webhook — SNS-subscribed entrypoint.

AWS's two-way SMS delivers inbound messages over an SNS topic subscription,
not a public HTTP endpoint — this mirrors
``app/lambdas/ses_notifications.py``'s SNS-parsing pattern rather than a
signed public webhook like WhatsApp's (see ``adapters/sms.py`` for why
signature verification is a no-op here).
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.services.omnichannel.connections import resolve_org_by_provider_account
from app.services.omnichannel.models import ChannelType

log = get_logger("omnichannel.webhooks.sms")


async def handle_notification(notification: dict[str, Any]) -> bool:
    """Process one parsed SNS two-way-SMS notification.

    Returns:
        True if it was enqueued (a known connection), False otherwise.

    Note: This is a Lambda-based entrypoint (distribution phase). At MVP (§12),
    SMS inbound arrives via an SNS topic subscription that the API process
    receives in-line; there is no separate Lambda.
    """
    destination = notification.get("destinationNumber")
    if not destination:
        return False

    org_id = await resolve_org_by_provider_account(ChannelType.SMS, destination)
    if org_id is None:
        log.info("sms.webhook.unknown_connection", extra={"destination": destination})
        return False

    # TODO: enqueue_inbound needs connection_id; this signature is incomplete for MVP
    # At MVP, the SNS subscription callback runs in the API process, not a Lambda
    return True
