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

import httpx
import pytest

from app.services.omnichannel.adapters._meta import (
    MetaGraphAdapter,
    extract_send_id,
    post_graph_api,
)
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


def test_every_meta_leaf_shares_the_signing_secret_key() -> None:
    assert WhatsAppAdapter().signing_secret_key == "app_secret"
    assert MessengerAdapter().signing_secret_key == "app_secret"
    assert InstagramAdapter().signing_secret_key == "app_secret"


_RealAsyncClient = httpx.AsyncClient


def _mock_client_factory(response: httpx.Response) -> object:
    """A stand-in for ``httpx.AsyncClient(timeout=...)`` that always answers
    with ``response``, via the real client's own MockTransport support --
    must go through ``_RealAsyncClient`` (captured before any monkeypatch),
    since ``_meta`` and this test module share the same ``httpx`` module
    object: patching ``httpx.AsyncClient`` anywhere patches it everywhere."""

    def _factory(**_kwargs: object) -> httpx.AsyncClient:
        return _RealAsyncClient(transport=httpx.MockTransport(lambda _request: response))

    return _factory


async def test_post_graph_api_raises_for_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "https://graph.facebook.com/v20.0/123/messages")
    response = httpx.Response(400, request=request, json={"error": "bad"})
    monkeypatch.setattr(
        "app.services.omnichannel.adapters._meta.httpx.AsyncClient",
        _mock_client_factory(response),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await post_graph_api("https://graph.facebook.com/v20.0/123/messages", {}, {})


async def test_post_graph_api_returns_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "https://graph.facebook.com/v20.0/123/messages")
    response = httpx.Response(200, request=request, json={"messages": [{"id": "wamid.1"}]})
    monkeypatch.setattr(
        "app.services.omnichannel.adapters._meta.httpx.AsyncClient",
        _mock_client_factory(response),
    )
    data = await post_graph_api("https://graph.facebook.com/v20.0/123/messages", {}, {})
    assert data == {"messages": [{"id": "wamid.1"}]}


def test_extract_send_id_success() -> None:
    assert (
        extract_send_id({"messages": [{"id": "wamid.1"}]}, "messages", 0, "id", channel="X")
        == "wamid.1"
    )
    assert extract_send_id({"message_id": "m.1"}, "message_id", channel="X") == "m.1"


@pytest.mark.parametrize(
    "data",
    [{}, {"messages": []}, {"messages": [{}]}, {"messages": [{"id": 123}]}],
)
def test_extract_send_id_rejects_malformed_shapes(data: dict[str, object]) -> None:
    with pytest.raises(ChannelAdapterError):
        extract_send_id(data, "messages", 0, "id", channel="X")
