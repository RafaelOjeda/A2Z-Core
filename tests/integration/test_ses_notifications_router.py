"""Integration tests for the SES notifications webhook (single-box MVP).

Replaces ``tests/integration/test_lambdas.py``'s SES half now that
``app/lambdas/ses_notifications.py`` is gone -- see
``app/routers/ses_notifications.py``'s module docstring for why an HTTP
endpoint needs to verify SNS's message signature itself (an IAM-invoked
Lambda got that boundary for free; an HTTP endpoint doesn't). The crypto
round-trip (sign with a real RSA key, verify against the matching cert) is
the load-bearing test here -- everything downstream (suppression, audit,
events) is already covered by ``core.email``'s own suite and unchanged.
"""

from __future__ import annotations

import base64
import datetime
import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app.core import email
from app.core.email import ServiceType
from app.main import app
from app.routers import ses_notifications as router_module
from app.routers.ses_notifications import SnsVerificationError, _canonical_string, _verify_signature

pytestmark = pytest.mark.integration

_FAKE_CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-fake.pem"


@pytest.fixture
def client(aws: None) -> TestClient:
    return TestClient(app)


def _generate_cert() -> tuple[rsa.RSAPrivateKey, bytes]:
    """A throwaway self-signed cert, standing in for AWS's real SNS signing cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM)


def _sign(key: rsa.RSAPrivateKey, payload: dict[str, Any]) -> str:
    canonical = _canonical_string(payload).encode("utf-8")
    signature = key.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def _signed_payload(
    key: rsa.RSAPrivateKey, *, msg_type: str, extra: dict[str, Any]
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Type": msg_type,
        "MessageId": "msg-1",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:a2z-ses-notifications",
        "Timestamp": "2026-07-27T00:00:00.000Z",
        "SignatureVersion": "2",
        "SigningCertURL": _FAKE_CERT_URL,
        **extra,
    }
    payload["Signature"] = _sign(key, payload)
    return payload


@pytest.fixture
def signing_key(monkeypatch: pytest.MonkeyPatch) -> rsa.RSAPrivateKey:
    """A fresh RSA keypair, with ``_fetch_signing_cert`` patched to return its
    matching self-signed cert -- so a test can sign a payload with this key
    and have the route's real verification path accept it, without any
    network fetch to a real AWS host."""
    key, cert_pem = _generate_cert()
    cert = x509.load_pem_x509_certificate(cert_pem)

    async def _fake_fetch(cert_url: str) -> x509.Certificate:
        assert cert_url == _FAKE_CERT_URL
        return cert

    monkeypatch.setattr(router_module, "_fetch_signing_cert", _fake_fetch)
    return key


async def test_canonical_string_field_order_for_notification() -> None:
    payload = {
        "Type": "Notification",
        "MessageId": "m1",
        "TopicArn": "t1",
        "Subject": "s1",
        "Timestamp": "ts1",
        "Message": "msg1",
        "SignatureVersion": "2",
        "Signature": "sig",
        "SigningCertURL": "url",
    }
    s = _canonical_string(payload)
    expected = (
        "Message\nmsg1\nMessageId\nm1\nSubject\ns1\n"
        "Timestamp\nts1\nTopicArn\nt1\nType\nNotification\n"
    )
    assert s == expected


async def test_canonical_string_omits_subject_when_absent() -> None:
    payload = {"Type": "Notification", "MessageId": "m1", "TopicArn": "t1", "Message": "msg1"}
    assert "Subject" not in _canonical_string(payload)


async def test_canonical_string_field_order_for_subscription_confirmation() -> None:
    payload = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "m1",
        "Token": "tok1",
        "TopicArn": "t1",
        "Message": "msg1",
        "SubscribeURL": "https://sub",
    }
    s = _canonical_string(payload)
    assert s == (
        "Message\nmsg1\nMessageId\nm1\nSubscribeURL\nhttps://sub\n"
        "Token\ntok1\nTopicArn\nt1\nType\nSubscriptionConfirmation\n"
    )


async def test_verify_signature_accepts_correctly_signed_payload(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    payload = _signed_payload(
        signing_key, msg_type="Notification", extra={"Message": json.dumps({"a": 1})}
    )
    await _verify_signature(payload)  # must not raise


async def test_verify_signature_rejects_tampered_payload(signing_key: rsa.RSAPrivateKey) -> None:
    payload = _signed_payload(
        signing_key, msg_type="Notification", extra={"Message": json.dumps({"a": 1})}
    )
    payload["Message"] = json.dumps({"a": 999})  # tamper after signing
    with pytest.raises(SnsVerificationError):
        await _verify_signature(payload)


async def test_verify_signature_rejects_untrusted_cert_host() -> None:
    """The host check in the real (unpatched) ``_fetch_signing_cert`` fires
    before any signature math, so this needs no real key/signature -- an
    untrusted ``SigningCertURL`` must be rejected on its own."""
    payload = {
        "Type": "Notification",
        "MessageId": "m1",
        "TopicArn": "t1",
        "Message": "{}",
        "SignatureVersion": "2",
        "SigningCertURL": "https://evil.example.com/cert.pem",
        "Signature": base64.b64encode(b"irrelevant").decode(),
    }
    with pytest.raises(SnsVerificationError):
        await _verify_signature(payload)


async def test_route_rejects_bad_signature(client: TestClient) -> None:
    payload = {
        "Type": "Notification",
        "MessageId": "m1",
        "TopicArn": "t1",
        "Timestamp": "ts",
        "Message": "{}",
        "SignatureVersion": "2",
        "SigningCertURL": _FAKE_CERT_URL,
        "Signature": base64.b64encode(b"not-a-real-signature").decode(),
    }
    resp = client.post("/webhooks/ses-notifications", content=json.dumps(payload))
    assert resp.status_code == 403


async def test_route_confirms_subscription(
    client: TestClient, signing_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    confirmed = AsyncMock(
        return_value=httpx.Response(200, request=httpx.Request("GET", "https://x"))
    )
    monkeypatch.setattr(router_module.clients.http_client(), "get", confirmed)

    payload = _signed_payload(
        signing_key,
        msg_type="SubscriptionConfirmation",
        extra={
            "Token": "tok-1",
            "Message": "You have chosen to subscribe...",
            "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&x=1",
        },
    )
    resp = client.post("/webhooks/ses-notifications", content=json.dumps(payload))
    assert resp.status_code == 200
    confirmed.assert_awaited_once()
    assert confirmed.call_args.args[0] == payload["SubscribeURL"]


async def test_route_confirmation_rejects_untrusted_subscribe_url(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    payload = _signed_payload(
        signing_key,
        msg_type="SubscriptionConfirmation",
        extra={
            "Token": "tok-1",
            "Message": "...",
            "SubscribeURL": "https://evil.example.com/confirm",
        },
    )
    resp = client.post("/webhooks/ses-notifications", content=json.dumps(payload))
    # Signature itself is valid (signed with the right key); the confirm
    # step's own host check is what must reject this.
    assert resp.status_code == 400


async def test_route_processes_bounce_notification(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    org_id = "ses-webhook-org"
    result = await email.send_email(
        org_id, ServiceType.INVOICING, "c@example.com", "Hi", "<p>hi</p>"
    )

    inner_message = json.dumps(
        {
            "notificationType": "Bounce",
            "mail": {"messageId": result.message_id},
            "bounce": {
                "bounceType": "Permanent",
                "bouncedRecipients": [{"emailAddress": "c@example.com"}],
            },
        }
    )
    payload = _signed_payload(
        signing_key, msg_type="Notification", extra={"Message": inner_message}
    )

    resp = client.post("/webhooks/ses-notifications", content=json.dumps(payload))
    assert resp.status_code == 200

    suppression = await email.get_suppression_list(org_id)
    assert "c@example.com" in suppression["bounced"]


async def test_route_processes_complaint_notification(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    org_id = "ses-webhook-org-2"
    result = await email.send_email(
        org_id, ServiceType.OMNICHANNEL, "s@example.com", "Hi", "<p>hi</p>"
    )

    inner_message = json.dumps(
        {
            "notificationType": "Complaint",
            "mail": {"messageId": result.message_id},
            "complaint": {"complainedRecipients": [{"emailAddress": "s@example.com"}]},
        }
    )
    payload = _signed_payload(
        signing_key, msg_type="Notification", extra={"Message": inner_message}
    )

    resp = client.post("/webhooks/ses-notifications", content=json.dumps(payload))
    assert resp.status_code == 200

    suppression = await email.get_suppression_list(org_id)
    assert "s@example.com" in suppression["complained"]


async def test_route_ignores_unresolvable_message(
    client: TestClient, signing_key: rsa.RSAPrivateKey
) -> None:
    """A message id we never sent (or whose event row aged out) is a no-op, not an error."""
    inner_message = json.dumps(
        {
            "notificationType": "Bounce",
            "mail": {"messageId": "never-sent"},
            "bounce": {
                "bounceType": "Permanent",
                "bouncedRecipients": [{"emailAddress": "x@example.com"}],
            },
        }
    )
    payload = _signed_payload(
        signing_key, msg_type="Notification", extra={"Message": inner_message}
    )
    resp = client.post("/webhooks/ses-notifications", content=json.dumps(payload))
    assert resp.status_code == 200
