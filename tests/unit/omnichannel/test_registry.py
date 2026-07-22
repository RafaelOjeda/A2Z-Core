"""Unit tests for the channel adapter registry (CLAUDE.md §5.2, §7).

Asserts the extensibility invariant directly: looking up a channel returns
something conforming to the ``ChannelAdapter`` Protocol, and an unknown
channel fails loudly rather than silently no-op'ing.
"""

from __future__ import annotations

import pytest

from app.services.omnichannel.adapters.base import ChannelAdapter
from app.services.omnichannel.adapters.email import EmailAdapter
from app.services.omnichannel.adapters.instagram import InstagramAdapter
from app.services.omnichannel.adapters.messenger import MessengerAdapter
from app.services.omnichannel.adapters.registry import get_adapter
from app.services.omnichannel.adapters.whatsapp import WhatsAppAdapter
from app.services.omnichannel.exceptions import ChannelAdapterError


def test_get_adapter_returns_email_adapter() -> None:
    adapter = get_adapter("email")
    assert isinstance(adapter, EmailAdapter)
    assert isinstance(adapter, ChannelAdapter)


def test_get_adapter_returns_whatsapp_adapter() -> None:
    adapter = get_adapter("whatsapp")
    assert isinstance(adapter, WhatsAppAdapter)
    assert isinstance(adapter, ChannelAdapter)


def test_get_adapter_returns_messenger_adapter() -> None:
    adapter = get_adapter("messenger")
    assert isinstance(adapter, MessengerAdapter)
    assert isinstance(adapter, ChannelAdapter)


def test_get_adapter_returns_instagram_adapter() -> None:
    adapter = get_adapter("instagram")
    assert isinstance(adapter, InstagramAdapter)
    assert isinstance(adapter, ChannelAdapter)


def test_get_adapter_unknown_channel_raises() -> None:
    with pytest.raises(ChannelAdapterError):
        get_adapter("carrier_pigeon")


def test_adapters_satisfy_protocol_at_runtime() -> None:
    # runtime_checkable only checks method presence, not signatures -- still
    # a useful guard that nobody drops a required method off an adapter.
    assert isinstance(EmailAdapter(), ChannelAdapter)
    assert isinstance(WhatsAppAdapter(), ChannelAdapter)
    assert isinstance(MessengerAdapter(), ChannelAdapter)
    assert isinstance(InstagramAdapter(), ChannelAdapter)
