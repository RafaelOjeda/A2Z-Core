"""Unit tests for the WhatsApp ChannelAdapter (CLAUDE.md §5.2, §13 Step 4).

Mocks ``_post_graph_api`` directly rather than the network -- these tests
verify the adapter's own logic (signature verification, payload mapping, the
v1 text-only / media-gap decisions), not Meta's API itself.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.omnichannel.adapters import whatsapp as whatsapp_module
from app.services.omnichannel.adapters.types import OutboundContent
from app.services.omnichannel.adapters.whatsapp import WhatsAppAdapter
from app.services.omnichannel.exceptions import ChannelAdapterError

adapter = WhatsAppAdapter()

_SECRET = "app-secret-shhh"


def _sign(raw_body: bytes, secret: str = _SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


async def test_verify_inbound_signature_valid() -> None:
    raw_body = b'{"entry": []}'
    headers = {"X-Hub-Signature-256": _sign(raw_body)}
    assert await adapter.verify_inbound_signature(raw_body, headers, _SECRET) is True


async def test_verify_inbound_signature_invalid() -> None:
    raw_body = b'{"entry": []}'
    headers = {"X-Hub-Signature-256": "sha256=deadbeef"}
    assert await adapter.verify_inbound_signature(raw_body, headers, _SECRET) is False


async def test_verify_inbound_signature_wrong_secret() -> None:
    raw_body = b'{"entry": []}'
    headers = {"X-Hub-Signature-256": _sign(raw_body, secret="a-different-secret")}
    assert await adapter.verify_inbound_signature(raw_body, headers, _SECRET) is False


async def test_verify_inbound_signature_missing_header() -> None:
    assert await adapter.verify_inbound_signature(b"{}", {}, _SECRET) is False


async def test_verify_inbound_signature_case_insensitive_header() -> None:
    raw_body = b'{"entry": []}'
    headers = {"x-hub-signature-256": _sign(raw_body)}
    assert await adapter.verify_inbound_signature(raw_body, headers, _SECRET) is True


async def test_normalize_inbound_text_message() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "15551234567", "profile": {"name": "Jane"}}],
                            "messages": [
                                {
                                    "from": "15551234567",
                                    "id": "wamid.ABC",
                                    "type": "text",
                                    "text": {"body": "Do you have this in blue?"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    messages = await adapter.normalize_inbound(payload)

    assert len(messages) == 1
    msg = messages[0]
    assert msg.external_id == "15551234567"
    assert msg.external_message_id == "wamid.ABC"
    assert msg.display_name == "Jane"
    assert msg.body_text == "Do you have this in blue?"


async def test_normalize_inbound_multiple_messages() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [],
                            "messages": [
                                {"from": "1", "id": "m1", "type": "text", "text": {"body": "hi"}},
                                {"from": "2", "id": "m2", "type": "text", "text": {"body": "yo"}},
                            ],
                        }
                    }
                ]
            }
        ]
    }
    messages = await adapter.normalize_inbound(payload)
    assert [m.external_message_id for m in messages] == ["m1", "m2"]


async def test_normalize_inbound_unsupported_media_type_still_persists() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [],
                            "messages": [
                                {
                                    "from": "1",
                                    "id": "m1",
                                    "type": "image",
                                    "image": {"id": "media-1"},
                                },
                            ],
                        }
                    }
                ]
            }
        ]
    }
    messages = await adapter.normalize_inbound(payload)

    assert len(messages) == 1
    assert messages[0].external_message_id == "m1"
    assert messages[0].body_text is not None
    assert "image" in messages[0].body_text
    assert messages[0].attachments == []


async def test_send_outbound_success(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_post = AsyncMock(return_value={"messages": [{"id": "wamid.OUT1"}]})
    monkeypatch.setattr(whatsapp_module, "_post_graph_api", mock_post)

    credentials = {"org_id": "org-a", "access_token": "tok", "phone_number_id": "123"}
    result = await adapter.send_outbound(
        "15551234567", OutboundContent(body_text="On its way!"), credentials
    )

    assert result.external_message_id == "wamid.OUT1"
    assert result.status == "sent"
    url, headers, payload = mock_post.call_args.args
    assert url == "https://graph.facebook.com/v20.0/123/messages"
    assert headers["Authorization"] == "Bearer tok"
    assert payload["to"] == "15551234567"
    assert payload["text"]["body"] == "On its way!"


async def test_send_outbound_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_post = AsyncMock()
    monkeypatch.setattr(whatsapp_module, "_post_graph_api", mock_post)

    with pytest.raises(ChannelAdapterError):
        await adapter.send_outbound(
            "155512345", OutboundContent(body_text="hi"), {"org_id": "org-a"}
        )
    mock_post.assert_not_called()


async def test_send_outbound_requires_text_body(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_post = AsyncMock()
    monkeypatch.setattr(whatsapp_module, "_post_graph_api", mock_post)

    credentials = {"org_id": "org-a", "access_token": "tok", "phone_number_id": "123"}
    with pytest.raises(ChannelAdapterError):
        await adapter.send_outbound("155512345", OutboundContent(), credentials)
    mock_post.assert_not_called()


async def test_send_outbound_wraps_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "http://x")
    response = httpx.Response(400, request=request)

    async def _raise(*args: object, **kwargs: object) -> dict[str, object]:
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr(whatsapp_module, "_post_graph_api", _raise)

    credentials = {"org_id": "org-a", "access_token": "tok", "phone_number_id": "123"}
    with pytest.raises(ChannelAdapterError):
        await adapter.send_outbound("155512345", OutboundContent(body_text="hi"), credentials)


async def test_interpret_delivery_webhook_maps_statuses() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {"id": "wamid.1", "status": "delivered"},
                                {"id": "wamid.2", "status": "read"},
                            ]
                        }
                    }
                ]
            }
        ]
    }
    updates = await adapter.interpret_delivery_webhook(payload)

    assert len(updates) == 2
    assert updates[0].external_message_id == "wamid.1"
    assert updates[0].status == "delivered"
    assert updates[1].status == "read"
