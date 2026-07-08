"""API Gateway -> Lambda entrypoint for the WhatsApp webhook.

Thin: parses the API Gateway proxy event shape and delegates to
``app/services/omnichannel/webhooks/whatsapp_webhook.py``. The app secret
and verify token come from Secrets Manager via ``core.secrets`` (platform-
level, not per-org — see that module's docstring).
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from app.core.logging import get_logger
from app.core.secrets import get_secret
from app.services.omnichannel.exceptions import WebhookSignatureError
from app.services.omnichannel.webhooks.whatsapp_webhook import handle_verification, handle_webhook

log = get_logger("lambda.omnichannel_whatsapp_webhook")

_PLATFORM_ORG = "_platform"  # app-level secret, not scoped to any one org
_PLATFORM_SERVICE = "omnichannel"


def _body_bytes(event: dict[str, Any]) -> bytes:
    body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode()


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """API Gateway proxy entrypoint. GET = verification handshake, POST = delivery."""

    async def _run() -> dict[str, Any]:
        method = event.get("httpMethod", "POST")
        secrets = await get_secret(_PLATFORM_ORG, _PLATFORM_SERVICE, "whatsapp_app")

        if method == "GET":
            params = event.get("queryStringParameters") or {}
            try:
                challenge = handle_verification(
                    params.get("hub.mode", ""),
                    params.get("hub.verify_token", ""),
                    params.get("hub.challenge", ""),
                    secrets["verify_token"],
                )
            except WebhookSignatureError:
                return {"statusCode": 403, "body": "verification failed"}
            return {"statusCode": 200, "body": challenge}

        raw_body = _body_bytes(event)
        headers = event.get("headers") or {}
        try:
            count = await handle_webhook(raw_body, headers, secrets["app_secret"])
        except WebhookSignatureError:
            log.error("whatsapp.webhook.bad_signature", extra={})
            return {"statusCode": 401, "body": "invalid signature"}

        log.info("whatsapp.webhook.enqueued", extra={"count": count})
        return {"statusCode": 200, "body": "ok"}

    return asyncio.run(_run())
