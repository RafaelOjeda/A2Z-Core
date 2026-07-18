"""HTTP-level tests for the Omni-Channel router (API review, 2026-07-18).

Complements the service-level suites (test_inbox.py, test_connections.py,
test_message_flow.py), which already cover the underlying logic in depth.
This file exists to prove the *router* wiring itself: status codes, response
envelopes, headers, and the two brand-new HTTP-only surfaces (the webhook
GET handshake, the ``Idempotency-Key`` header) that only exist at this layer.

Runs the real app via ``TestClient`` -- moto AWS + fakeredis (``aws``
fixture) for Core (membership/secrets/events). Postgres access is via
``TestClient`` requests only, deliberately: Starlette's ``TestClient`` drives
the ASGI app from its own event loop (a background-thread portal), which is
never the same loop as an async test function's. The omnichannel conftest's
autouse ``_fresh_engine`` fixture builds Core's async Postgres engine in the
*test's* loop; mixing that engine into a request served on the portal's loop
raises asyncpg "attached to a different loop" errors. ``_reset_db_engine_for_
testclient`` below resets the cached engine around every test in this file so
each side (this file's own async seeding, and the portal's request handling)
gets its own engine bound to its own loop -- see db.py::reset_engine.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core import clients
from app.main import app
from app.services.omnichannel import db
from app.services.omnichannel.models import ChannelIdentity, Conversation

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_db_engine_for_testclient() -> Iterator[None]:
    db.reset_engine()
    yield
    db.reset_engine()


@pytest.fixture
def client(aws: None) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_org(client: TestClient, token: str, name: str = "Acme") -> str:
    resp = client.post("/v1/core/orgs", json={"name": name}, headers=_auth(token))
    assert resp.status_code == 201
    org_id: str = resp.json()["org_id"]
    return org_id


async def _seed_secret(org_id: str, key: str, value: dict[str, str]) -> None:
    await clients.run_aws(
        clients.secretsmanager().create_secret,
        Name=f"a2z/{org_id}/omnichannel/{key}",
        SecretString=json.dumps(value),
    )


def _seed_secret_sync(org_id: str, key: str, value: dict[str, str]) -> None:
    asyncio.run(_seed_secret(org_id, key, value))


async def _seed_conversation(org_id: str) -> str:
    async with db.get_session_context() as session:
        identity = ChannelIdentity(
            org_id=org_id, channel_type="whatsapp", external_id="15551234567", display_name="Jane"
        )
        session.add(identity)
        await session.flush()
        conversation = Conversation(
            org_id=org_id,
            customer_identity_id=identity.id,
            status="open",
            last_message_at=datetime.now(timezone.utc),
        )
        session.add(conversation)
        await session.commit()
        return conversation.id


def _seed_conversation_sync(org_id: str) -> str:
    """Seed via a throwaway event loop, then hand the Postgres engine back to
    the caller's loop -- see the module docstring."""
    conversation_id = asyncio.run(_seed_conversation(org_id))
    db.reset_engine()
    return conversation_id


def _create_whatsapp_connection(client: TestClient, org_id: str, token: str) -> str:
    resp = client.post(
        f"/v1/omnichannel/orgs/{org_id}/connections",
        json={
            "channel_type": "whatsapp",
            "display_name": "Acme WhatsApp",
            "provider_account_id": "pn-123",
            "credentials_secret_key": "whatsapp-main",
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201
    connection_id: str = resp.json()["id"]
    return connection_id


# --- connections CRUD ---


def test_connections_crud_round_trip(client: TestClient, make_token: Callable[..., str]) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, token, "Acme")
    _seed_secret_sync(org_id, "whatsapp-main", {"app_secret": "s", "verify_token": "vt"})

    connection_id = _create_whatsapp_connection(client, org_id, token)

    list_resp = client.get(f"/v1/omnichannel/orgs/{org_id}/connections", headers=_auth(token))
    assert list_resp.status_code == 200
    assert [c["id"] for c in list_resp.json()["items"]] == [connection_id]

    get_resp = client.get(
        f"/v1/omnichannel/orgs/{org_id}/connections/{connection_id}", headers=_auth(token)
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == connection_id

    delete_resp = client.delete(
        f"/v1/omnichannel/orgs/{org_id}/connections/{connection_id}", headers=_auth(token)
    )
    assert delete_resp.status_code == 204

    get_after_delete = client.get(
        f"/v1/omnichannel/orgs/{org_id}/connections/{connection_id}", headers=_auth(token)
    )
    assert get_after_delete.json()["status"] == "disabled"


def test_create_connection_forbidden_for_non_admin(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    owner = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, owner)
    member = make_token("auth0|member", "member@acme.com")
    client.post(
        f"/v1/core/orgs/{org_id}/members",
        json={"user_id": "auth0|member", "role": "member"},
        headers=_auth(owner),
    )

    resp = client.post(
        f"/v1/omnichannel/orgs/{org_id}/connections",
        json={
            "channel_type": "whatsapp",
            "display_name": "x",
            "provider_account_id": "pn-1",
            "credentials_secret_key": "k",
        },
        headers=_auth(member),
    )
    assert resp.status_code == 403


def test_create_connection_rejects_unregistered_channel(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, token)

    resp = client.post(
        f"/v1/omnichannel/orgs/{org_id}/connections",
        json={
            "channel_type": "sms",
            "display_name": "x",
            "provider_account_id": "pn-1",
            "credentials_secret_key": "k",
        },
        headers=_auth(token),
    )
    assert resp.status_code == 400


# --- webhook GET verification handshake ---


def test_webhook_verification_handshake(client: TestClient, make_token: Callable[..., str]) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, token)
    _seed_secret_sync(org_id, "whatsapp-main", {"app_secret": "s", "verify_token": "vt"})
    connection_id = _create_whatsapp_connection(client, org_id, token)

    resp = client.get(
        f"/v1/omnichannel/webhooks/whatsapp/{connection_id}",
        params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"},
    )
    assert resp.status_code == 200
    assert resp.text == "42"
    assert resp.headers["content-type"].startswith("text/plain")


def test_webhook_verification_handshake_wrong_token_rejected(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, token)
    _seed_secret_sync(org_id, "whatsapp-main", {"app_secret": "s", "verify_token": "vt"})
    connection_id = _create_whatsapp_connection(client, org_id, token)

    resp = client.get(
        f"/v1/omnichannel/webhooks/whatsapp/{connection_id}",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"},
    )
    assert resp.status_code == 502  # ChannelAdapterError


# --- pagination envelope + idempotency-key + status codes ---


def test_list_conversations_returns_page_envelope(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, token)
    _seed_conversation_sync(org_id)

    resp = client.get(f"/v1/omnichannel/orgs/{org_id}/conversations", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and "next_cursor" in body
    assert len(body["items"]) == 1


def test_send_reply_status_codes_and_idempotency(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, token)
    conversation_id = _seed_conversation_sync(org_id)

    first = client.post(
        f"/v1/omnichannel/orgs/{org_id}/conversations/{conversation_id}/messages",
        json={"body_text": "hello"},
        headers={**_auth(token), "Idempotency-Key": "req-1"},
    )
    assert first.status_code == 201
    message_id = first.json()["message_id"]

    replay = client.post(
        f"/v1/omnichannel/orgs/{org_id}/conversations/{conversation_id}/messages",
        json={"body_text": "hello (retried)"},
        headers={**_auth(token), "Idempotency-Key": "req-1"},
    )
    assert replay.status_code == 200
    assert replay.json()["message_id"] == message_id

    no_key = client.post(
        f"/v1/omnichannel/orgs/{org_id}/conversations/{conversation_id}/messages",
        json={"body_text": "another one"},
        headers=_auth(token),
    )
    assert no_key.status_code == 201
    assert no_key.json()["message_id"] != message_id


def test_routing_config_bad_strategy_is_400(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, token)

    resp = client.put(
        f"/v1/omnichannel/orgs/{org_id}/routing-config",
        json={"strategy": "round_robin"},
        headers=_auth(token),
    )
    assert resp.status_code == 400
