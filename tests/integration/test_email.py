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


@pytest.mark.parametrize("bad", ["", "no-at-sign", "a@b", "two@@example.com", "spa ce@example.com"])
async def test_invalid_address_rejected(aws: None, bad: str) -> None:
    """Design §2.3: send_email raises InvalidAddressError before touching SES."""
    from app.core.exceptions import InvalidAddressError

    with pytest.raises(InvalidAddressError):
        await email.send_email("org-x", ServiceType.INVOICING, bad, "Hi", "<p>hi</p>")


async def test_config_set_gets_sns_event_destination(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md §8: each lazily-created config set gets a Bounce/Complaint ->
    SNS event destination, so bounces actually reach the ses_notifications
    Lambda in AWS. Idempotent on the second send (Redis cache path)."""
    from app import config
    from app.core import clients

    topic_arn = clients.sns().create_topic(Name="a2z-ses-notifications")["TopicArn"]
    monkeypatch.setenv("SES_NOTIFICATIONS_TOPIC_ARN", topic_arn)
    config.settings.cache_clear()
    try:
        org_id = "dest-org"
        await email.send_email(org_id, ServiceType.INVOICING, "a@example.com", "Hi", "<p>hi</p>")

        described = clients.ses().describe_configuration_set(
            ConfigurationSetName=f"{org_id}-invoicing",
            ConfigurationSetAttributeNames=["eventDestinations"],
        )
        destinations = described["EventDestinations"]
        assert len(destinations) == 1
        dest = destinations[0]
        assert dest["Enabled"] is True
        assert set(dest["MatchingEventTypes"]) == {"bounce", "complaint"}
        assert dest["SNSDestination"]["TopicARN"] == topic_arn

        # Second send: config set + destination already exist — must not error.
        r2 = await email.send_email(
            org_id, ServiceType.INVOICING, "b@example.com", "Hi", "<p>hi</p>"
        )
        assert r2.status == EmailStatus.SENT
    finally:
        config.settings.cache_clear()
