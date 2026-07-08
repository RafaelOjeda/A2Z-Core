"""Unit tests for the webhook entrypoint modules (org resolution + enqueue),
mocking resolve_org_by_provider_account and enqueue_inbound directly rather
than hitting Postgres/SQS — the persistence/queue behavior itself is covered
by tests/integration/omnichannel/test_message_flow.py."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest

from app.services.omnichannel.exceptions import WebhookSignatureError
from app.services.omnichannel.models import ChannelType
from app.services.omnichannel.webhooks import ses_inbound, sms_webhook, whatsapp_webhook


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_handle_verification_returns_challenge_on_match() -> None:
    challenge = whatsapp_webhook.handle_verification("subscribe", "tok", "chal-1", "tok")
    assert challenge == "chal-1"


def test_handle_verification_raises_on_mismatch() -> None:
    with pytest.raises(WebhookSignatureError):
        whatsapp_webhook.handle_verification("subscribe", "wrong", "chal-1", "tok")


async def test_handle_webhook_raises_on_bad_signature() -> None:
    body = json.dumps({"entry": []}).encode()
    with pytest.raises(WebhookSignatureError):
        await whatsapp_webhook.handle_webhook(body, {"X-Hub-Signature-256": "sha256=bad"}, "secret")


async def test_handle_webhook_enqueues_only_known_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {"value": {"metadata": {"phone_number_id": "known"}, "messages": [{"id": "1"}]}}
                ]
            },
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "unknown"},
                            "messages": [{"id": "2"}],
                        }
                    }
                ]
            },
        ]
    }
    body = json.dumps(payload).encode()

    async def fake_resolve(channel_type: ChannelType, provider_account_id: str) -> str | None:
        return "org-a" if provider_account_id == "known" else None

    fake_enqueue = AsyncMock()
    monkeypatch.setattr(whatsapp_webhook, "resolve_org_by_provider_account", fake_resolve)
    monkeypatch.setattr(whatsapp_webhook, "enqueue_inbound", fake_enqueue)

    count = await whatsapp_webhook.handle_webhook(
        body, {"X-Hub-Signature-256": _sign("secret", body)}, "secret"
    )

    assert count == 1
    fake_enqueue.assert_called_once()
    assert fake_enqueue.call_args.args[0] == ChannelType.WHATSAPP
    assert fake_enqueue.call_args.args[1] == "org-a"


async def test_sms_handle_notification_enqueues_for_known_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_resolve = AsyncMock(return_value="org-a")
    fake_enqueue = AsyncMock()
    monkeypatch.setattr(sms_webhook, "resolve_org_by_provider_account", fake_resolve)
    monkeypatch.setattr(sms_webhook, "enqueue_inbound", fake_enqueue)

    notification = {"destinationNumber": "+15550001111", "messageBody": "hi"}
    result = await sms_webhook.handle_notification(notification)

    assert result is True
    fake_enqueue.assert_called_once_with(ChannelType.SMS, "org-a", notification)


async def test_sms_handle_notification_skips_unknown_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sms_webhook, "resolve_org_by_provider_account", AsyncMock(return_value=None)
    )
    fake_enqueue = AsyncMock()
    monkeypatch.setattr(sms_webhook, "enqueue_inbound", fake_enqueue)

    result = await sms_webhook.handle_notification(
        {"destinationNumber": "+1555", "messageBody": "hi"}
    )

    assert result is False
    fake_enqueue.assert_not_called()


async def test_sms_handle_notification_ignores_missing_destination() -> None:
    result = await sms_webhook.handle_notification({"messageBody": "hi"})
    assert result is False


async def test_ses_inbound_handle_s3_object_resolves_org_and_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "customer@example.com"
    msg["To"] = "sales@acme.com"
    msg.set_content("hi")
    raw = bytes(msg.as_bytes())

    class _FakeBody:
        def read(self) -> bytes:
            return raw

    class _FakeS3:
        def get_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
            return {"Body": _FakeBody()}

    monkeypatch.setattr(
        "app.services.omnichannel.webhooks.ses_inbound.clients.s3", lambda: _FakeS3()
    )
    fake_resolve = AsyncMock(return_value="org-a")
    fake_enqueue = AsyncMock()
    monkeypatch.setattr(ses_inbound, "resolve_org_by_provider_account", fake_resolve)
    monkeypatch.setattr(ses_inbound, "enqueue_inbound", fake_enqueue)

    result = await ses_inbound.handle_s3_object("bucket", "key")

    assert result is True
    fake_resolve.assert_called_once_with(ChannelType.EMAIL, "sales@acme.com")
    fake_enqueue.assert_called_once()
    assert fake_enqueue.call_args.args[0] == ChannelType.EMAIL
    assert fake_enqueue.call_args.args[1] == "org-a"


async def test_ses_inbound_skips_unknown_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "customer@example.com"
    msg["To"] = "unknown@nowhere.com"
    msg.set_content("hi")
    raw = bytes(msg.as_bytes())

    class _FakeBody:
        def read(self) -> bytes:
            return raw

    class _FakeS3:
        def get_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
            return {"Body": _FakeBody()}

    monkeypatch.setattr(
        "app.services.omnichannel.webhooks.ses_inbound.clients.s3", lambda: _FakeS3()
    )
    monkeypatch.setattr(
        ses_inbound, "resolve_org_by_provider_account", AsyncMock(return_value=None)
    )
    fake_enqueue = AsyncMock()
    monkeypatch.setattr(ses_inbound, "enqueue_inbound", fake_enqueue)

    result = await ses_inbound.handle_s3_object("bucket", "key")

    assert result is False
    fake_enqueue.assert_not_called()
