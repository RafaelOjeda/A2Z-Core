"""Unit tests for the channel adapter registry (CLAUDE.md §5.2, §7).

Asserts the extensibility invariant directly: looking up a channel returns
something conforming to the ``ChannelAdapter`` Protocol, and an unknown
channel fails loudly rather than silently no-op'ing.
"""

from __future__ import annotations

import pytest

from app.config import RATE_LIMITS
from app.services.omnichannel.adapters.base import ChannelAdapter
from app.services.omnichannel.adapters.email import EmailAdapter
from app.services.omnichannel.adapters.instagram import InstagramAdapter
from app.services.omnichannel.adapters.messenger import MessengerAdapter
from app.services.omnichannel.adapters.registry import _REGISTRY, get_adapter
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


def test_registry_contents_exact() -> None:
    """Pins the registry to exactly the 4 live channels -- guards against SMS
    (built but deliberately unregistered, known-issues.md #1) being flipped on
    by accident, and catches a channel silently missing its registry line."""
    assert set(_REGISTRY) == {"email", "whatsapp", "messenger", "instagram"}


def test_every_credentialed_channel_has_a_rate_limit() -> None:
    """handlers.py looks up RATE_LIMITS[f"omnichannel.{channel}.send"] and
    silently applies no limit if the key is missing (§7) -- a config gap that
    fails open, not closed. Every registered channel except email (which has
    no channel-specific limit by design -- core.email enforces "email.send")
    must have an explicit entry."""
    for channel_type, adapter in _REGISTRY.items():
        if not adapter.supported_features.requires_credentials:
            continue
        assert f"omnichannel.{channel_type}.send" in RATE_LIMITS


def test_every_adapter_declares_a_signing_secret_key() -> None:
    for adapter in _REGISTRY.values():
        assert isinstance(adapter.signing_secret_key, str)
