"""End-to-end API tests via FastAPI TestClient (moto + fakeredis).

Exercises the DoD scenario (CLAUDE.md §15): boot the app, /health checks
DynamoDB + Redis, and the admin router creates an org, adds a member, changes
settings, and sends a test email end-to-end.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def client(aws: None) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dynamodb"] == "ok"
    assert body["redis"] == "ok"


def test_missing_token_is_401(client: TestClient) -> None:
    assert client.post("/core/orgs", json={"name": "X"}).status_code == 401


def test_full_admin_flow(client: TestClient, make_token: Callable[..., str]) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    h = _auth(token)

    # Create org (caller becomes owner).
    resp = client.post("/core/orgs", json={"name": "Acme"}, headers=h)
    assert resp.status_code == 201
    org_id = resp.json()["org_id"]

    # List members -> just the owner.
    resp = client.get(f"/core/orgs/{org_id}/members", headers=h)
    assert resp.status_code == 200 and len(resp.json()) == 1

    # Add a member.
    resp = client.post(
        f"/core/orgs/{org_id}/members",
        json={"user_id": "auth0|sarah", "role": "member"},
        headers=h,
    )
    assert resp.status_code == 201

    # Change settings.
    resp = client.patch(
        f"/core/orgs/{org_id}/settings",
        json={"changes": {"timezone": "America/New_York"}},
        headers=h,
    )
    assert resp.status_code == 200
    assert resp.json()["timezone"] == "America/New_York"

    # Send a test email.
    resp = client.post(
        "/core/email/send",
        json={
            "org_id": org_id,
            "service_type": "invoicing",
            "to": "client@example.com",
            "subject": "Invoice #1",
            "body_html": "<p>hi</p>",
        },
        headers=h,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"


def test_non_member_forbidden(client: TestClient, make_token: Callable[..., str]) -> None:
    owner = make_token("auth0|owner2", "o2@acme.com")
    org_id = client.post("/core/orgs", json={"name": "Org2"}, headers=_auth(owner)).json()[
        "org_id"
    ]
    # A different user is not a member -> 404 from require_member.
    stranger = make_token("auth0|stranger", "s@x.com")
    resp = client.get(f"/core/orgs/{org_id}/members", headers=_auth(stranger))
    assert resp.status_code == 404
