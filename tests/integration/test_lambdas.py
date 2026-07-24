"""Integration tests for the out-of-band Lambda handlers (moto)."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import settings as app_settings
from app.core import clients, email
from app.core._ddb import from_item, to_item
from app.core.email import ServiceType
from app.lambdas import cognito_post_confirm, ses_notifications

pytestmark = pytest.mark.integration


async def _get_user_item(sub: str) -> dict | None:
    resp = await clients.run_aws(
        clients.dynamodb().get_item,
        TableName=app_settings().tables["membership"],
        Key=to_item({"PK": f"USER#{sub}", "SK": "METADATA"}),
    )
    return from_item(resp["Item"]) if resp.get("Item") else None


def _cognito_event(sub: str, email_addr: str) -> dict:
    return {"request": {"userAttributes": {"sub": sub, "email": email_addr}}}


def test_post_confirm_creates_user_and_is_idempotent(aws: None) -> None:
    event = _cognito_event("auth0|new", "new@example.com")
    assert cognito_post_confirm.handler(event) == event  # returns event for Cognito
    # Second call must not raise (Cognito may retry).
    assert cognito_post_confirm.handler(event) == event

    item = asyncio.run(_get_user_item("auth0|new"))
    assert item is not None and item["email"] == "new@example.com"


def test_post_confirm_missing_attrs_still_returns_event(aws: None) -> None:
    event = {"request": {"userAttributes": {}}}
    assert cognito_post_confirm.handler(event) == event


def test_ses_bounce_notification_suppresses(aws: None) -> None:
    org_id = "lambda-org"
    result = asyncio.run(
        email.send_email(org_id, ServiceType.INVOICING, "c@example.com", "Hi", "<p>hi</p>")
    )
    sns_event = {
        "Records": [
            {
                "Sns": {
                    "Message": json.dumps({
                        "notificationType": "Bounce",
                        "mail": {"messageId": result.message_id},
                        "bounce": {
                            "bounceType": "Permanent",
                            "bouncedRecipients": [{"emailAddress": "c@example.com"}],
                        },
                    })
                }
            }
        ]
    }
    out = ses_notifications.handler(sns_event)
    assert out["status"] == "ok"

    suppression = asyncio.run(email.get_suppression_list(org_id))
    assert "c@example.com" in suppression["bounced"]


def test_ses_complaint_notification_suppresses(aws: None) -> None:
    org_id = "lambda-org-2"
    result = asyncio.run(
        email.send_email(org_id, ServiceType.OMNICHANNEL, "s@example.com", "Hi", "<p>hi</p>")
    )
    sns_event = {
        "Records": [
            {
                "Sns": {
                    "Message": json.dumps({
                        "notificationType": "Complaint",
                        "mail": {"messageId": result.message_id},
                        "complaint": {"complainedRecipients": [{"emailAddress": "s@example.com"}]},
                    })
                }
            }
        ]
    }
    ses_notifications.handler(sns_event)
    suppression = asyncio.run(email.get_suppression_list(org_id))
    assert "s@example.com" in suppression["complained"]
