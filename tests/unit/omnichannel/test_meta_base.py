"""Unit tests for the shared Meta base adapter (CLAUDE.md §5.2, §7, §15).

``MetaGraphAdapter`` owns the two pieces every Meta channel (WhatsApp,
Messenger, Instagram) shares byte-for-byte: ``X-Hub-Signature-256`` inbound
signature verification and the ``hub.challenge`` subscription handshake.
Verified once here through a concrete leaf (``MessengerAdapter``), plus a
guard that all three leaves inherit the base.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.services.omnichannel.adapters._meta import MetaGraphAdapter
from app.services.omnichannel.adapters.instagram import InstagramAdapter
from app.services.omnichannel.adapters.messenger import MessengerAdapter
from app.services.omnichannel.adapters.whatsapp import WhatsAppAdapter
from app.services.omnichannel.exceptions import ChannelAdapterError

adapter = MessengerAdapter()

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


async def test_verify_subscription_success_echoes_challenge() -> None:
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "tok-123",
        "hub.challenge": "echo-me",
    }
    result = await adapter.verify_subscription(params, {"verify_token": "tok-123"})
    assert result == "echo-me"


async def test_verify_subscription_wrong_token_raises() -> None:
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong",
        "hub.challenge": "echo-me",
    }
    with pytest.raises(ChannelAdapterError):
        await adapter.verify_subscription(params, {"verify_token": "tok-123"})


async def test_verify_subscription_missing_mode_raises() -> None:
    params = {"hub.verify_token": "tok-123", "hub.challenge": "echo-me"}
    with pytest.raises(ChannelAdapterError):
        await adapter.verify_subscription(params, {"verify_token": "tok-123"})


def test_all_meta_leaves_inherit_the_base() -> None:
    # The shared signature/handshake methods only reach a channel if it's on
    # the base -- guard that every Meta leaf actually is.
    assert issubclass(WhatsAppAdapter, MetaGraphAdapter)
    assert issubclass(MessengerAdapter, MetaGraphAdapter)
    assert issubclass(InstagramAdapter, MetaGraphAdapter)
