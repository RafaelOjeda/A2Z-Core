"""SES bounce/complaint webhook — SNS HTTPS delivery (root CLAUDE.md §8).

Single-box MVP (docs/architecture/single-box-mvp.md): this used to be
``app/lambdas/ses_notifications.py``, a Lambda subscribed to the SNS topic.
There is no Lambda anymore — no separate compute to subscribe — so SNS
delivers straight to this HTTP route instead (``aws_sns_topic_subscription``
with ``protocol = "https"``, see infra/modules/ses). Two things the Lambda
subscription got for free that an HTTP endpoint has to do itself:

1. **Subscription confirmation.** SNS won't deliver real notifications until
   this endpoint fetches the ``SubscribeURL`` it's sent on first subscribe
   (or whenever a subscription needs reconfirming). Miss this and the
   subscription silently never activates.
2. **Authenticity.** An IAM-invoked Lambda is reachable by SNS *and nothing
   else*, by construction (``aws_lambda_permission`` scoped to the topic
   ARN). An HTTP endpoint is reachable by anyone who finds the URL. Without
   verifying SNS's message signature, anyone could POST a fabricated bounce/
   complaint and poison an org's suppression list -- a real customer-facing
   DoS (their legitimate emails stop sending). So every request's signature
   is verified before its payload is trusted; this is the replacement for
   the IAM boundary the Lambda used to provide, not optional hardening.

Message processing itself (``_process_notification``) is unchanged from the
old Lambda -- same suppression + audit + event-publish path in
``core.email``; only the transport and the trust check are new.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509 import Certificate, load_pem_x509_certificate
from fastapi import APIRouter, HTTPException, Request

from app.core import clients
from app.core.cache import TTLCache
from app.core.email import (
    _handle_bounce_notification,
    _handle_complaint_notification,
    resolve_org_for_message,
)
from app.core.logging import get_logger

router = APIRouter(tags=["ses-notifications"])
log = get_logger("router.ses_notifications")

# SNS signing certs are long-lived and AWS-rotated on its own schedule; a
# day's staleness is a non-issue and saves an httpx round trip per webhook
# call. Same idiom as auth.py's JWKS cache.
_CERT_CACHE_TTL_SECONDS = 24 * 3600
_cert_cache = TTLCache()

# AWS always serves signing certs from exactly this host pattern. Validated
# *before* fetching the URL -- never fetch an attacker-supplied host, which
# would turn "verify the signature" into an SSRF primitive.
_SIGNING_CERT_HOST_RE = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")

# Field order matters -- this is SNS's canonical "string to sign", not a
# convenience ordering. See:
# https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
_NOTIFICATION_FIELDS = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
_SUBSCRIPTION_FIELDS = (
    "Message",
    "MessageId",
    "SubscribeURL",
    "Timestamp",
    "Token",
    "TopicArn",
    "Type",
)


class SnsVerificationError(Exception):
    """The message's signature (or its signing cert URL) failed verification."""


def _canonical_string(payload: dict[str, Any]) -> str:
    fields = _SUBSCRIPTION_FIELDS if payload.get("Token") else _NOTIFICATION_FIELDS
    parts: list[str] = []
    for field in fields:
        if field not in payload:
            continue  # Subject is optional on Notification messages.
        parts.append(field)
        parts.append(str(payload[field]))
    return "".join(f"{p}\n" for p in parts)


async def _fetch_signing_cert(cert_url: str) -> Certificate:
    if not cert_url.startswith("https://") or not _SIGNING_CERT_HOST_RE.match(
        httpx.URL(cert_url).host
    ):
        raise SnsVerificationError(
            f"Refusing to fetch signing cert from untrusted host: {cert_url}"
        )

    cached_pem = _cert_cache.get(cert_url)
    if cached_pem is None:
        try:
            resp = await clients.http_client().get(cert_url, timeout=5.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # Any fetch failure (network, timeout, 404, ...) means we can't
            # verify -- treat that the same as "didn't verify", not a 500.
            raise SnsVerificationError(f"Failed to fetch signing cert: {exc}") from exc
        cached_pem = resp.text
        _cert_cache.set(cert_url, cached_pem, ttl_seconds=_CERT_CACHE_TTL_SECONDS)
    try:
        return load_pem_x509_certificate(cached_pem.encode("utf-8"))
    except ValueError as exc:
        raise SnsVerificationError(f"Signing cert is not a valid certificate: {exc}") from exc


async def _verify_signature(payload: dict[str, Any]) -> None:
    """Raise :class:`SnsVerificationError` if the message's signature doesn't check out."""
    cert_url = payload.get("SigningCertURL")
    signature_b64 = payload.get("Signature")
    if not cert_url or not signature_b64:
        raise SnsVerificationError("Missing SigningCertURL/Signature")

    cert = await _fetch_signing_cert(cert_url)
    signature = base64.b64decode(signature_b64)
    canonical = _canonical_string(payload).encode("utf-8")
    # SignatureVersion "2" = SHA256; "1" (default/absent) = SHA1 -- both are
    # still what real SNS sends depending on topic config, so both must verify.
    digest = hashes.SHA256() if payload.get("SignatureVersion") == "2" else hashes.SHA1()
    public_key = cert.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        # SNS always signs with RSA; anything else means a cert we shouldn't trust anyway.
        raise SnsVerificationError(f"Signing cert has unexpected key type: {type(public_key)}")
    try:
        public_key.verify(signature, canonical, padding.PKCS1v15(), digest)
    except Exception as exc:  # noqa: BLE001 -- any verification failure is untrusted
        raise SnsVerificationError(f"Signature verification failed: {exc}") from exc


async def _confirm_subscription(payload: dict[str, Any]) -> None:
    subscribe_url = payload["SubscribeURL"]
    if not subscribe_url.startswith("https://") or not _SIGNING_CERT_HOST_RE.match(
        httpx.URL(subscribe_url).host
    ):
        raise SnsVerificationError(f"Refusing to confirm via untrusted host: {subscribe_url}")
    resp = await clients.http_client().get(subscribe_url, timeout=5.0)
    resp.raise_for_status()
    log.info("ses_notifications.subscription_confirmed", extra={"topic": payload.get("TopicArn")})


async def _process_notification(notification: dict[str, Any]) -> None:
    n_type = notification.get("notificationType") or notification.get("eventType")
    mail = notification.get("mail", {})
    message_id = mail.get("messageId")
    if not message_id:
        return

    org_id = await resolve_org_for_message(message_id)
    if not org_id:
        # Event row may have aged out, or message wasn't ours.
        log.info("ses_notifications.unresolved", extra={"message_id": message_id})
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
        log.info("ses_notifications.ignored", extra={"type": str(n_type)})


@router.post("/webhooks/ses-notifications")
async def ses_notifications(request: Request) -> dict[str, str]:
    """SNS -> HTTPS entrypoint for SES bounce/complaint notifications.

    Every message type SNS sends over an HTTPS subscription is handled:
    ``SubscriptionConfirmation`` (fetch ``SubscribeURL`` to activate),
    ``Notification`` (the actual bounce/complaint, same processing as the
    old Lambda), and ``UnsubscribeConfirmation`` (logged, no action needed).

    Raises:
        HTTPException: 403 if the message's signature doesn't verify --
            SNS retries deliveries it doesn't get a 2xx for, but a forged
            request should never reach suppression-list writes.
    """
    payload = json.loads(await request.body())
    try:
        await _verify_signature(payload)
    except SnsVerificationError as exc:
        log.error("ses_notifications.verification_failed", extra={"error": str(exc)})
        raise HTTPException(status_code=403, detail="Signature verification failed") from exc

    msg_type = payload.get("Type")
    if msg_type == "SubscriptionConfirmation":
        try:
            await _confirm_subscription(payload)
        except SnsVerificationError as exc:
            # The signature checked out, but the SubscribeURL itself points
            # somewhere untrusted -- shouldn't happen from real SNS, but
            # never fetch it if it does.
            log.error("ses_notifications.confirm_rejected", extra={"error": str(exc)})
            raise HTTPException(status_code=400, detail="Untrusted SubscribeURL") from exc
    elif msg_type == "Notification":
        try:
            notification = json.loads(payload.get("Message", "{}"))
        except json.JSONDecodeError:
            log.error("ses_notifications.bad_json", extra={})
            return {"status": "ok"}
        await _process_notification(notification)
    else:
        log.info("ses_notifications.unhandled_type", extra={"type": str(msg_type)})

    return {"status": "ok"}
