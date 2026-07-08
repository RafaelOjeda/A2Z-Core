"""S3 -> Lambda entrypoint for inbound email.

Thin: parses the S3 ObjectCreated event shape and delegates to
``app/services/omnichannel/webhooks/ses_inbound.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.services.omnichannel.webhooks.ses_inbound import handle_s3_object

log = get_logger("lambda.omnichannel_ses_inbound")


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    async def _run() -> None:
        for record in event.get("Records", []):
            s3_info = record.get("s3", {})
            bucket = s3_info.get("bucket", {}).get("name")
            key = s3_info.get("object", {}).get("key")
            if not bucket or not key:
                continue
            await handle_s3_object(bucket, key)

    asyncio.run(_run())
    return {"status": "ok"}
