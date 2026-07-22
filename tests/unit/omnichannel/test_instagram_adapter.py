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

from unittest.mock import AsyncMock

import pytest

from app.services.omnichannel.adapters import messenger as messenger_module
from app.services.omnichannel.adapters.instagram import InstagramAdapter
from app.services.omnichannel.adapters.types import OutboundContent
from app.services.omnichannel.exceptions import ChannelAdapterError

adapter = InstagramAdapter()


async def test_send_outbound_uses_ig_id_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_post = AsyncMock(return_value={"recipient_id": "IGUSER", "message_id": "ig.OUT1"})
    monkeypatch.setattr(messenger_module, "_post_graph_api", mock_post)

    credentials = {"org_id": "org-a", "page_access_token": "tok", "ig_id": "IG777"}
    result = await adapter.send_outbound(
        "IGUSER", OutboundContent(body_text="Thanks for the DM!"), credentials
    )

    assert result.external_message_id == "ig.OUT1"
    url, _headers, payload = mock_post.call_args.args
    assert url == "https://graph.facebook.com/v20.0/IG777/messages"
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
