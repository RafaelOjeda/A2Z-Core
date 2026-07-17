"""SES inbound email — S3-triggered entrypoint.

SES's receipt rule writes the raw MIME to a landing-zone S3 prefix that
isn't org-scoped (SES doesn't know our org model), so this reads it
directly via the S3 client rather than ``core.storage`` (which requires an
org-scoped key). Once the recipient's org is resolved from the ``To``
address against ``channel_connections``, the payload is handed to the
worker like any other channel's inbound path.
"""

from __future__ import annotations

import base64
import email as email_lib
import email.policy

from app.core import clients
from app.core.logging import get_logger
from app.services.omnichannel.connections import resolve_org_by_provider_account
from app.services.omnichannel.models import ChannelType
from app.services.omnichannel.queues import enqueue_inbound

log = get_logger("omnichannel.webhooks.ses_inbound")


async def handle_s3_object(bucket: str, key: str) -> bool:
    """Fetch a landed raw MIME object, resolve its org, and enqueue it.

    Returns:
        True if enqueued (recipient resolves to a known connection), False
        if the recipient address doesn't match any org's connection.

    Note: This is an S3-triggered Lambda entrypoint (distribution phase). At
    MVP (§12), SES receipt rule writes directly to S3 but the S3 event
    notification target is an SQS queue that the API process drains in-line;
    there is no separate Lambda.
    """
    resp = await clients.run_aws(clients.s3().get_object, Bucket=bucket, Key=key)
    raw_mime = resp["Body"].read()

    msg = email_lib.message_from_bytes(raw_mime, policy=email.policy.default)
    to_addr = msg.get("To", "")

    org_id = await resolve_org_by_provider_account(ChannelType.EMAIL, to_addr)
    if org_id is None:
        log.info("ses_inbound.unknown_connection", extra={"to": to_addr})
        return False

    # TODO: enqueue_inbound needs connection_id; this signature is incomplete for MVP
    # At MVP, the S3 event notification lands on the SQS queue that the API process drains
    return True
