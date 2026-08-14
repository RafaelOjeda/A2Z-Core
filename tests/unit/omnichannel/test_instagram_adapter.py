"""Unit tests for the Instagram ChannelAdapter (CLAUDE.md §5.2, §15).

Instagram DMs run on the same Messenger Platform as Facebook Messenger, so the
normalize/delivery behavior is already proven in ``test_messenger_adapter.py``.
These focus on what's actually Instagram-specific: it uses the ``ig_id``
credential (not ``page_id``) for the Send API path, and still inherits the
shared ``messaging[]`` inbound shape.

Note the send seam: ``send_outbound`` is defined on ``MessengerPlatformAdapter``
in the ``messenger`` module, so the ``_post_graph_api`` global to patch lives
there even when the instance is an ``InstagramAdapter``.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.omnichannel.adapters import messenger as messenger_module
from app.services.omnichannel.adapters._meta import GRAPH_API_BASE
from app.services.omnichannel.adapters.instagram import InstagramAdapter
from app.services.omnichannel.adapters.types import OutboundContent
from app.services.omnichannel.exceptions import ChannelAdapterError

adapter = InstagramAdapter()

_SECRET = "ig-app-secret"


def _sign(raw_body: bytes, secret: str = _SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


async def test_send_outbound_uses_ig_id_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_post = AsyncMock(return_value={"recipient_id": "IGUSER", "message_id": "ig.OUT1"})
    monkeypatch.setattr(messenger_module, "_post_graph_api", mock_post)

    credentials = {"org_id": "org-a", "page_access_token": "tok", "ig_id": "IG777"}
    result = await adapter.send_outbound(
        "IGUSER", OutboundContent(body_text="Thanks for the DM!"), credentials
    )

    assert result.external_message_id == "ig.OUT1"
    url, _headers, payload = mock_post.call_args.args
    assert url == f"{GRAPH_API_BASE}/IG777/messages"
    assert payload["recipient"]["id"] == "IGUSER"


async def test_send_outbound_page_id_is_not_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    # Instagram keys on ig_id -- a page_id-only bundle must be rejected, proving
    # the account-id override actually took effect.
    mock_post = AsyncMock()
    monkeypatch.setattr(messenger_module, "_post_graph_api", mock_post)

    credentials = {"org_id": "org-a", "page_access_token": "tok", "page_id": "PAGE123"}
    with pytest.raises(ChannelAdapterError):
        await adapter.send_outbound("IGUSER", OutboundContent(body_text="hi"), credentials)
    mock_post.assert_not_called()


async def test_normalize_inbound_shares_messenger_shape() -> None:
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "IG777",
                "messaging": [
                    {"sender": {"id": "IGUSER"}, "message": {"mid": "ig.m1", "text": "hi there"}},
                    {"sender": {"id": "IGUSER"}, "read": {"watermark": 1}},  # skipped
                ],
            }
        ],
    }
    messages = await adapter.normalize_inbound(payload)

    assert len(messages) == 1
    assert messages[0].external_id == "IGUSER"
    assert messages[0].external_message_id == "ig.m1"
    assert messages[0].body_text == "hi there"


def test_instagram_requires_a_stored_credential() -> None:
    assert adapter.supported_features.requires_credentials is True


def test_instagram_read_receipts_is_false() -> None:
    """Inherited from MessengerPlatformAdapter -- same watermark-only gap."""
    assert adapter.supported_features.read_receipts is False


async def test_verify_inbound_signature_through_ig_leaf() -> None:
    """Proves the inheritance actually reaches the IG leaf instance, not just
    MetaGraphAdapter in the abstract (test_meta_base.py covers the shared
    logic itself)."""
    raw_body = b'{"entry": []}'
    headers = {"X-Hub-Signature-256": _sign(raw_body)}
    assert await adapter.verify_inbound_signature(raw_body, headers, _SECRET) is True
    assert await adapter.verify_inbound_signature(raw_body, {}, _SECRET) is False


async def test_verify_subscription_through_ig_leaf() -> None:
    params = {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "echo-me"}
    assert await adapter.verify_subscription(params, {"verify_token": "tok"}) == "echo-me"

    with pytest.raises(ChannelAdapterError):
        await adapter.verify_subscription(params, {"verify_token": "wrong"})


async def test_interpret_delivery_webhook_through_ig_leaf() -> None:
    payload = {
        "entry": [{"messaging": [{"sender": {"id": "IGUSER"}, "delivery": {"mids": ["ig.m1"]}}]}]
    }
    updates = await adapter.interpret_delivery_webhook(payload)
    assert [(u.external_message_id, u.status) for u in updates] == [("ig.m1", "delivered")]


async def test_send_outbound_wraps_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "http://x")
    response = httpx.Response(400, request=request)

    async def _raise(*args: object, **kwargs: object) -> dict[str, object]:
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr(messenger_module, "_post_graph_api", _raise)

    credentials = {"org_id": "org-a", "page_access_token": "tok", "ig_id": "IG777"}
    with pytest.raises(ChannelAdapterError):
        await adapter.send_outbound("IGUSER", OutboundContent(body_text="hi"), credentials)


@pytest.mark.parametrize("bad_response", [{}, {"recipient_id": "IGUSER"}])
async def test_send_outbound_rejects_malformed_response(
    monkeypatch: pytest.MonkeyPatch, bad_response: dict[str, object]
) -> None:
    """Same guard as WhatsApp/Messenger's -- see adapters/_meta.py::extract_send_id."""
    mock_post = AsyncMock(return_value=bad_response)
    monkeypatch.setattr(messenger_module, "_post_graph_api", mock_post)

    credentials = {"org_id": "org-a", "page_access_token": "tok", "ig_id": "IG777"}
    with pytest.raises(ChannelAdapterError):
        await adapter.send_outbound("IGUSER", OutboundContent(body_text="hi"), credentials)
