"""Integration tests for core.email (moto SES + DynamoDB), incl. Design §4.2."""

from __future__ import annotations

import pytest

from app.core import email
from app.core.email import EmailStatus, ServiceType
from app.core.exceptions import RateLimitError, SuppressionListError

pytestmark = pytest.mark.integration


async def test_send_email_and_track_delivery(aws: None) -> None:
    """Design §4.2: send -> bounce -> suppression -> blocked resend -> unsuppress."""
    org_id = "test-org-456"
    result = await email.send_email(
        org_id=org_id,
        service_type=ServiceType.INVOICING,
        to="client@example.com",
        subject="Invoice #1054",
        body_html="<p>Amount due: $1,500</p>",
        metadata={"invoice_id": "1054"},
    )
    assert result.status == EmailStatus.SENT
    assert result.message_id

    # Logged in email-events.
    assert await email.get_email_status(result.message_id) == EmailStatus.SENT

    # Simulate SES bounce (SNS -> Lambda path).
    await email._handle_bounce_notification(
        org_id=org_id,
        message_id=result.message_id,
        to="client@example.com",
        bounce_type="Permanent",
    )
    assert await email.get_email_status(result.message_id) == EmailStatus.BOUNCED

    suppression = await email.get_suppression_list(org_id)
    assert "client@example.com" in suppression["bounced"]

    # Resend to the suppressed address is blocked.
    with pytest.raises(SuppressionListError):
        await email.send_email(
            org_id, ServiceType.INVOICING, "client@example.com", "Invoice #1055", "..."
        )

    # Unsuppress, then it works again.
    await email.unsuppress_email(org_id, "client@example.com")
    result2 = await email.send_email(
        org_id, ServiceType.INVOICING, "client@example.com", "Invoice #1055", "<p>hi</p>"
    )
    assert result2.status == EmailStatus.SENT


async def test_complaint_suppresses(aws: None) -> None:
    org_id = "org-complaint"
    r = await email.send_email(
        org_id, ServiceType.OMNICHANNEL, "spam-reporter@example.com", "Hi", "<p>hi</p>"
    )
    await email._handle_complaint_notification(org_id, r.message_id, "spam-reporter@example.com")
    suppression = await email.get_suppression_list(org_id)
    assert "spam-reporter@example.com" in suppression["complained"]


async def test_rate_limit_enforced(aws: None) -> None:
    org_id = "org-ratelimit"
    limit, _ = email.rate_limit.limits_for("email.send")
    for i in range(limit):
        await email.send_email(
            org_id, ServiceType.INVOICING, f"r{i}@example.com", "Hi", "<p>hi</p>"
        )
    with pytest.raises(RateLimitError):
        await email.send_email(org_id, ServiceType.INVOICING, "over@example.com", "Hi", "<p>hi</p>")


async def test_send_with_attachment(aws: None) -> None:
    org_id = "org-attach"
    r = await email.send_email(
        org_id,
        ServiceType.INVOICING,
        "client@example.com",
        "Invoice",
        "<p>see attached</p>",
        attachments=[
            {
                "filename": "invoice.pdf",
                "content": b"%PDF-1.4 fake",
                "mime_type": "application/pdf",
            }
        ],
    )
    assert r.status == EmailStatus.SENT


async def test_suppression_is_per_org(aws: None) -> None:
    # Bounce in org-a must not suppress the same address in org-b.
    r = await email.send_email(
        "org-a", ServiceType.INVOICING, "shared@example.com", "Hi", "<p>hi</p>"
    )
    await email._handle_bounce_notification(
        "org-a", r.message_id, "shared@example.com", "Permanent"
    )
    # org-b can still send to the same address.
    r2 = await email.send_email(
        "org-b", ServiceType.INVOICING, "shared@example.com", "Hi", "<p>hi</p>"
    )
    assert r2.status == EmailStatus.SENT


async def test_unknown_message_status_raises(aws: None) -> None:
    from app.core.exceptions import EmailError

    with pytest.raises(EmailError):
        await email.get_email_status("does-not-exist")
