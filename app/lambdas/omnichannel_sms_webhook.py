"""SNS -> Lambda entrypoint for two-way SMS.

Thin: parses the SNS record shape (same pattern as
``app/lambdas/ses_notifications.py``) and delegates to
``app/services/omnichannel/webhooks/sms_webhook.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.logging import get_logger
from app.services.omnichannel.webhooks.sms_webhook import handle_notification

log = get_logger("lambda.omnichannel_sms_webhook")


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    async def _run() -> None:
        for record in event.get("Records", []):
            raw = record.get("Sns", {}).get("Message", "{}")
            try:
                notification = json.loads(raw)
            except json.JSONDecodeError:
                log.error("sms.webhook.bad_json", extra={})
                continue
            await handle_notification(notification)

    asyncio.run(_run())
    return {"status": "ok"}
