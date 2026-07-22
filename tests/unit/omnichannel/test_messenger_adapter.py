"""Unit tests for the Messenger ChannelAdapter (CLAUDE.md §5.2, §15).

Mocks ``_post_graph_api`` directly rather than the network -- these verify the
adapter's own logic (the ``messaging[]`` normalize incl. receipt/echo
skipping, the Send API payload shape, the v1 text-only / media-gap
decisions), not Meta's API. Signature/handshake live on the base and are
covered by ``test_meta_base.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.omnichannel.adapters import messenger as messenger_module
from app.services.omnichannel.adapters.messenger import MessengerAdapter
from app.services.omnichannel.adapters.types import OutboundContent
from app.services.omnichannel.exceptions import ChannelAdapterError

adapter = MessengerAdapter()


async def test_normalize_inbound_text_message() -> None:
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "PAGE",
                "messaging": [
                    {
                        "sender": {"id": "USER1"},
                        "recipient": {"id": "PAGE"},
                        "message": {"mid": "m.abc", "text": "Do you have this in blue?"},
                    }
                ],
            }
        ],
    }
    messages = await adapter.normalize_inbound(payload)

    assert len(messages) == 1
    msg = messages[0]
    assert msg.external_id == "USER1"
    assert msg.external_message_id == "m.abc"
    assert msg.body_text == "Do you have this in blue?"
    # Messenger webhooks carry no inline profile name.
    assert msg.display_name is None


async def test_normalize_inbound_multiple_messages() -> None:
    payload = {
        "entry": [
            {
                "messaging": [
                    {"sender": {"id": "1"}, "message": {"mid": "m1", "text": "hi"}},
                    {"sender": {"id": "2"}, "message": {"mid": "m2", "text": "yo"}},
                ]
            }
        ]
    }
    messages = await adapter.normalize_inbound(payload)
    assert [m.external_message_id for m in messages] == ["m1", "m2"]


async def test_normalize_inbound_skips_delivery_read_and_echo() -> None:
    # The crux: Messenger mixes customer messages with delivery/read receipts
    # and echoes of the page's own outbound in one messaging[] array. Only the
    # genuine customer message may survive normalize -- the worker persists
    # whatever this returns.
    payload = {
        "entry": [
            {
                "messaging": [
                    {"sender": {"id": "USER1"}, "message": {"mid": "m.real", "text": "hello"}},
                    {
                        "sender": {"id": "PAGE"},
                        "message": {"mid": "m.echo", "text": "auto-reply", "is_echo": True},
                    },
                    {"sender": {"id": "USER1"}, "delivery": {"mids": ["m.real"], "watermark": 1}},
                    {"sender": {"id": "USER1"}, "read": {"watermark": 2}},
                ]
            }
        ]
    }
    messages = await adapter.normalize_inbound(payload)

    assert len(messages) == 1
    assert messages[0].external_message_id == "m.real"


async def test_normalize_inbound_attachment_persists_with_placeholder() -> None:
    payload = {
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "USER1"},
                        "message": {
                            "mid": "m.img",
                            "attachments": [{"type": "image", "payload": {"url": "http://x"}}],
                        },
                    }
                ]
            }
        ]
    }
    messages = await adapter.normalize_inbound(payload)

    assert len(messages) == 1
    assert messages[0].external_message_id == "m.img"
    assert messages[0].body_text is not None
    assert "attachment" in messages[0].body_text
    assert messages[0].attachments == []


async def test_send_outbound_success(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_post = AsyncMock(return_value={"recipient_id": "USER1", "message_id": "mid.OUT1"})
    monkeypatch.setattr(messenger_module, "_post_graph_api", mock_post)

    credentials = {"org_id": "org-a", "page_access_token": "tok", "page_id": "PAGE123"}
    result = await adapter.send_outbound(
        "USER1", OutboundContent(body_text="On its way!"), credentials
    )

    assert result.external_message_id == "mid.OUT1"
    assert result.status == "sent"
    url, headers, payload = mock_post.call_args.args
    assert url == "https://graph.facebook.com/v20.0/PAGE123/messages"
    assert headers["Authorization"] == "Bearer tok"
    assert payload["recipient"]["id"] == "USER1"
    assert payload["message"]["text"] == "On its way!"
    assert payload["messaging_type"] == "RESPONSE"


async def test_send_outbound_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_post = AsyncMock()
    monkeypatch.setattr(messenger_module, "_post_graph_api", mock_post)

    with pytest.raises(ChannelAdapterError):
        await adapter.send_outbound("USER1", OutboundContent(body_text="hi"), {"org_id": "org-a"})
    mock_post.assert_not_called()


async def test_send_outbound_requires_text_body(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_post = AsyncMock()
    monkeypatch.setattr(messenger_module, "_post_graph_api", mock_post)

    credentials = {"org_id": "org-a", "page_access_token": "tok", "page_id": "PAGE123"}
    with pytest.raises(ChannelAdapterError):
        await adapter.send_outbound("USER1", OutboundContent(), credentials)
    mock_post.assert_not_called()


async def test_send_outbound_wraps_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "http://x")
    response = httpx.Response(400, request=request)

    async def _raise(*args: object, **kwargs: object) -> dict[str, object]:
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr(messenger_module, "_post_graph_api", _raise)

    credentials = {"org_id": "org-a", "page_access_token": "tok", "page_id": "PAGE123"}
    with pytest.raises(ChannelAdapterError):
        await adapter.send_outbound("USER1", OutboundContent(body_text="hi"), credentials)


async def test_interpret_delivery_webhook_maps_mids() -> None:
    payload = {
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "USER1"},
                        "delivery": {"mids": ["m.1", "m.2"], "watermark": 9},
                    },
                    {
                        "sender": {"id": "USER1"},
                        "read": {"watermark": 9},
                    },  # watermark-only: skipped
                ]
            }
        ]
    }
    updates = await adapter.interpret_delivery_webhook(payload)

    assert [(u.external_message_id, u.status) for u in updates] == [
        ("m.1", "delivered"),
        ("m.2", "delivered"),
    ]


def test_messenger_requires_a_stored_credential() -> None:
    """connections.py's self-service branch relies on this default staying True."""
    assert adapter.supported_features.requires_credentials is True
