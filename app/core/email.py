"""Email — multi-tenant sending via SES with per-org isolation (Design §2.3).

``send_email`` composes the most of any Core module. In order (CLAUDE.md §8):
resolve sender from settings -> ensure the org/service SES config set exists
(lazy, cached) -> check suppression -> rate-limit -> SES send -> record in
email-events -> audit. Bounces/complaints (delivered out-of-band via SNS) flow
through :func:`_handle_bounce_notification` / :func:`_handle_complaint_notification`,
which suppress the address and publish ``email.bounced`` / ``email.complained``.

Suppression scope decision (CLAUDE.md §8): tracked **per org**, shared across
that org's services. The table keeps a nullable ``service_type`` so we *can*
narrow to per-service later without a migration. Do not "fix" this to per-service
by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Any

from botocore.exceptions import ClientError

from app.config import settings as app_settings
from app.core import clients, rate_limit
from app.core._ddb import from_item, to_item, to_value
from app.core.audit import ActionType, log_audit
from app.core.events import publish_event
from app.core.exceptions import EmailError, SuppressionListError
from app.core.logging import get_logger
from app.core.settings import get_org_settings

log = get_logger("core.email")

_EMAIL_EVENT_RETENTION = timedelta(days=90)  # CLAUDE.md §11
_CONFIG_SET_CACHE_TTL = 24 * 3600
# Local/dev fallback when an org hasn't set a verified domain yet. In prod the
# org must configure a verified domain (Design §2.3 step 1).
_DEFAULT_DOMAIN = "example.com"


class ServiceType(str, Enum):
    INVOICING = "invoicing"
    OMNICHANNEL = "omnichannel"
    APPOINTMENTS = "appointments"
    EXPENSES = "expenses"


class EmailStatus(str, Enum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    COMPLAINED = "complained"
    REJECTED = "rejected"


@dataclass
class EmailResult:
    message_id: str
    status: EmailStatus
    timestamp: datetime
    external_message_id: str


def _events_table() -> str:
    return app_settings().tables["email_events"]


def _suppression_table() -> str:
    return app_settings().tables["suppression"]


def _config_set_name(org_id: str, service_type: ServiceType) -> str:
    return f"{org_id}-{service_type.value}"


async def _ensure_config_set(org_id: str, service_type: ServiceType) -> str:
    """Lazily create the org/service SES config set; cache 'exists' in Redis.

    One config set per ``{org_id}-{service_type}`` isolates each org's sending
    reputation (Design §2.3 / CLAUDE.md §8). We cache existence to avoid a
    describe/create on every send.
    """
    name = _config_set_name(org_id, service_type)
    redis = clients.redis_client()
    cache_key = f"ses:cs:{name}"
    if await redis.get(cache_key):
        return name
    try:
        await clients.run_aws(
            clients.ses().create_configuration_set,
            ConfigurationSet={"Name": name},
        )
        log.info("ses.configset.created", extra={"org_id": org_id, "config_set": name})
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("ConfigurationSetAlreadyExists", "AlreadyExists"):
            # Non-fatal for the cache, but surface unexpected failures.
            log.info("ses.configset.error", extra={"config_set": name, "code": code})
    await redis.set(cache_key, "1", ex=_CONFIG_SET_CACHE_TTL)
    return name


async def _is_suppressed(org_id: str, to: str) -> bool:
    resp = await clients.run_aws(
        clients.dynamodb().get_item,
        TableName=_suppression_table(),
        Key=to_item({"org_id": org_id, "email": to}),
    )
    return bool(resp.get("Item"))


def _html_to_text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def _sender(domain: str, service_type: ServiceType, sender_name: str) -> str:
    addr = f"{service_type.value}@{domain}"
    return f"{sender_name} <{addr}>" if sender_name else addr


async def send_email(
    org_id: str,
    service_type: ServiceType,
    to: str,
    subject: str,
    body_html: str,
    body_text: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    reply_to: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> EmailResult:
    """Send an email on behalf of an org (Design §2.3).

    Raises:
        SuppressionListError: ``to`` is on the org's suppression list.
        RateLimitError: Org exceeded its email rate limit (50/hour).
        EmailError: Any SES failure.

    Performance: < 500ms (we wait for SES acceptance).
    """
    org = await get_org_settings(org_id)
    domain = org.domain or _DEFAULT_DOMAIN
    source = _sender(domain, service_type, org.sender_name)
    reply_to = reply_to or f"{service_type.value}@{domain}"

    await _ensure_config_set(org_id, service_type)

    if await _is_suppressed(org_id, to):
        raise SuppressionListError(f"{to} is suppressed for org {org_id}")

    limit, window = rate_limit.limits_for("email.send")
    await rate_limit.check_and_increment(
        org_id, "email.send", limit=limit, window_seconds=window
    )

    # Local/dev: ensure the sending domain is a verified SES identity so sends
    # succeed against moto/LocalStack. In prod the domain is verified once at
    # org onboarding, not here.
    if not app_settings().is_prod:
        try:
            await clients.run_aws(clients.ses().verify_domain_identity, Domain=domain)
        except ClientError:
            pass

    text = body_text or _html_to_text(body_html)
    config_set = _config_set_name(org_id, service_type)
    try:
        message_id = await _ses_send(
            source, to, subject, body_html, text, reply_to, config_set, attachments
        )
    except ClientError as exc:
        raise EmailError(f"SES send failed: {exc}") from exc

    now = datetime.now(timezone.utc)
    await _record_event(org_id, message_id, to, service_type, subject, metadata, now)
    await log_audit(
        org_id, "system", ActionType.EMAIL_SENT, "email", message_id,
        {"to": to, "subject": subject, "service_type": service_type.value},
    )
    log.info("email.sent", extra={"org_id": org_id, "service_type": service_type.value})

    return EmailResult(
        message_id=message_id, status=EmailStatus.SENT, timestamp=now,
        external_message_id=message_id,
    )


async def _ses_send(
    source: str,
    to: str,
    subject: str,
    body_html: str,
    body_text: str,
    reply_to: str,
    config_set: str,
    attachments: list[dict[str, Any]] | None,
) -> str:
    """Send via SES, using SendRawEmail when there are attachments."""
    if attachments:
        raw = _build_mime(source, to, subject, body_html, body_text, reply_to, attachments)
        resp = await clients.run_aws(
            clients.ses().send_raw_email,
            Source=source,
            Destinations=[to],
            RawMessage={"Data": raw},
            ConfigurationSetName=config_set,
        )
        return str(resp["MessageId"])

    resp = await clients.run_aws(
        clients.ses().send_email,
        Source=source,
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Html": {"Data": body_html}, "Text": {"Data": body_text}},
        },
        ReplyToAddresses=[reply_to],
        ConfigurationSetName=config_set,
    )
    return str(resp["MessageId"])


def _build_mime(
    source: str,
    to: str,
    subject: str,
    body_html: str,
    body_text: str,
    reply_to: str,
    attachments: list[dict[str, Any]],
) -> bytes:
    msg = MIMEMultipart("mixed")
    msg["Subject"], msg["From"], msg["To"], msg["Reply-To"] = subject, source, to, reply_to
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_text, "plain"))
    alt.attach(MIMEText(body_html, "html"))
    msg.attach(alt)
    for att in attachments:
        part = MIMEApplication(att["content"])
        part.add_header("Content-Disposition", "attachment", filename=att["filename"])
        if att.get("mime_type"):
            part.set_type(att["mime_type"])
        msg.attach(part)
    return msg.as_bytes()


async def _record_event(
    org_id: str,
    message_id: str,
    to: str,
    service_type: ServiceType,
    subject: str,
    metadata: dict[str, Any] | None,
    now: datetime,
) -> None:
    item = {
        "message_id": message_id,
        "org_id": org_id,
        "timestamp": now.isoformat(),
        "to": to,
        "service_type": service_type.value,
        "status": EmailStatus.SENT.value,
        "subject": subject,
        "metadata": metadata or {},
        "ttl": int((now + _EMAIL_EVENT_RETENTION).timestamp()),
    }
    await clients.run_aws(
        clients.dynamodb().put_item, TableName=_events_table(), Item=to_item(item)
    )


async def get_email_status(message_id: str) -> EmailStatus:
    """Return the current delivery status of an email (Design §2.3). < 50ms."""
    resp = await clients.run_aws(
        clients.dynamodb().get_item,
        TableName=_events_table(),
        Key=to_item({"message_id": message_id}),
    )
    if not resp.get("Item"):
        raise EmailError(f"Unknown message_id: {message_id}")
    return EmailStatus(from_item(resp["Item"])["status"])


async def get_suppression_list(org_id: str) -> dict[str, list[str]]:
    """Return the org's bounce/complaint lists (Design §2.3). < 100ms."""
    resp = await clients.run_aws(
        clients.dynamodb().query,
        TableName=_suppression_table(),
        KeyConditionExpression="org_id = :o",
        ExpressionAttributeValues={":o": to_value(org_id)},
    )
    out: dict[str, list[str]] = {"bounced": [], "complained": []}
    for item in resp.get("Items", []):
        data = from_item(item)
        bucket = "bounced" if data.get("reason") == "bounce" else "complained"
        out[bucket].append(data["email"])
    return out


async def unsuppress_email(org_id: str, email: str) -> None:
    """Remove an address from the suppression list (Design §2.3)."""
    await clients.run_aws(
        clients.dynamodb().delete_item,
        TableName=_suppression_table(),
        Key=to_item({"org_id": org_id, "email": email}),
    )
    await log_audit(
        org_id, "system", ActionType.EMAIL_UNSUPPRESSED, "email", email, {"email": email}
    )
    log.info("email.unsuppressed", extra={"org_id": org_id})


async def _suppress(
    org_id: str, email: str, reason: str, bounce_type: str | None = None
) -> None:
    now = datetime.now(timezone.utc)
    item: dict[str, Any] = {
        "org_id": org_id,
        "email": email,
        "reason": reason,
        "timestamp": now.isoformat(),
        # Nullable on purpose — org-level today, per-service-capable later.
        "service_type": None,
    }
    if bounce_type:
        item["bounce_type"] = bounce_type
    await clients.run_aws(
        clients.dynamodb().put_item, TableName=_suppression_table(), Item=to_item(item)
    )


async def _set_event_status(message_id: str, status: EmailStatus) -> None:
    try:
        await clients.run_aws(
            clients.dynamodb().update_item,
            TableName=_events_table(),
            Key=to_item({"message_id": message_id}),
            UpdateExpression="SET #s = :s",
            ConditionExpression="attribute_exists(message_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=to_item({":s": status.value}),
        )
    except ClientError:
        # Event row may have aged out (90d TTL); status update is best-effort.
        pass


async def _handle_bounce_notification(
    org_id: str, message_id: str, to: str, bounce_type: str
) -> None:
    """Process an SES hard/soft bounce: suppress + mark + publish. Idempotent."""
    await _suppress(org_id, to, "bounce", bounce_type)
    await _set_event_status(message_id, EmailStatus.BOUNCED)
    await log_audit(
        org_id, "system", ActionType.EMAIL_BOUNCED, "email", message_id,
        {"to": to, "bounce_type": bounce_type},
    )
    await publish_event(
        org_id, "email.bounced",
        {"email": to, "bounce_type": bounce_type, "message_id": message_id},
    )
    log.info("email.bounced", extra={"org_id": org_id, "bounce_type": bounce_type})


async def _handle_complaint_notification(
    org_id: str, message_id: str, to: str
) -> None:
    """Process an SES complaint: suppress + mark + publish. Idempotent."""
    await _suppress(org_id, to, "complaint")
    await _set_event_status(message_id, EmailStatus.COMPLAINED)
    await log_audit(
        org_id, "system", ActionType.EMAIL_COMPLAINED, "email", message_id, {"to": to}
    )
    await publish_event(
        org_id, "email.complained", {"email": to, "message_id": message_id}
    )
    log.info("email.complained", extra={"org_id": org_id})
