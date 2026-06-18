"""SES bounce/complaint handler — SNS-subscribed Lambda (CLAUDE.md §8).

Subscribed to the SNS topic(s) wired to each SES config set's event
destination. Parses the SES notification, resolves the org from the stored
message id, writes suppression (per org), and publishes
``email.bounced`` / ``email.complained``. Idempotent — SNS can redeliver.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.email import (
    _handle_bounce_notification,
    _handle_complaint_notification,
    resolve_org_for_message,
)
from app.core.logging import get_logger

log = get_logger("lambda.ses_notifications")


async def _process_notification(notification: dict[str, Any]) -> None:
    n_type = notification.get("notificationType") or notification.get("eventType")
    mail = notification.get("mail", {})
    message_id = mail.get("messageId")
    if not message_id:
        return

    org_id = await resolve_org_for_message(message_id)
    if not org_id:
        # Event row may have aged out, or message wasn't ours.
        log.info("ses.notification.unresolved", extra={"message_id": message_id})
        return

    if n_type == "Bounce":
        bounce = notification.get("bounce", {})
        bounce_type = bounce.get("bounceType", "Permanent")
        for r in bounce.get("bouncedRecipients", []):
            await _handle_bounce_notification(org_id, message_id, r["emailAddress"], bounce_type)
    elif n_type == "Complaint":
        complaint = notification.get("complaint", {})
        for r in complaint.get("complainedRecipients", []):
            await _handle_complaint_notification(org_id, message_id, r["emailAddress"])
    else:
        log.info("ses.notification.ignored", extra={"type": str(n_type)})


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """SNS -> Lambda entrypoint. Processes each SES notification record."""

    async def _run() -> None:
        for record in event.get("Records", []):
            raw = record.get("Sns", {}).get("Message", "{}")
            try:
                notification = json.loads(raw)
            except json.JSONDecodeError:
                log.error("ses.notification.bad_json", extra={})
                continue
            await _process_notification(notification)

    asyncio.run(_run())
    return {"status": "ok"}
