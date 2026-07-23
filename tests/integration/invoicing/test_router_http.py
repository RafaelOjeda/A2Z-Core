"""HTTP-level tests for the Invoicing router (§9).

Runs the real app via ``TestClient`` -- moto AWS + fakeredis (``aws``
fixture) for Core (membership/settings/storage/email), real Postgres for
the invoicing schema. Mirrors
``tests/integration/omnichannel/test_router_http.py``'s pattern exactly,
including the engine-reset dance around ``TestClient``'s own event loop --
see that file's module docstring for why.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.invoicing import db

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


def _add_member(client: TestClient, org_id: str, owner_token: str, user_id: str, role: str) -> None:
    resp = client.post(
        f"/v1/core/orgs/{org_id}/members",
        json={"user_id": user_id, "role": role},
        headers=_auth(owner_token),
    )
    assert resp.status_code == 201


_INVOICE_BODY = {
    "customer_email": "jane@example.com",
    "customer_name": "Jane Smith",
    "customer_company": "Acme Co",
    "invoice_date": "2026-07-22",
    "due_date": "2026-08-21",
    "payment_terms": "net-30",
    "line_items": [{"description": "Consulting", "quantity": "3", "unit_price_cents": 15000}],
    "tax_cents": 4050,
    "discount_cents": 0,
    "notes": "Thanks!",
}


def _create_invoice(client: TestClient, org_id: str, token: str) -> str:
    resp = client.post(
        f"/v1/invoicing/orgs/{org_id}/invoices", json=_INVOICE_BODY, headers=_auth(token)
    )
    assert resp.status_code == 201
    invoice_id: str = resp.json()["invoice_id"]
    return invoice_id


# --- CRUD round trip ---


def test_invoice_crud_round_trip(client: TestClient, make_token: Callable[..., str]) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, token, "Acme")

    invoice_id = _create_invoice(client, org_id, token)

    list_resp = client.get(f"/v1/invoicing/orgs/{org_id}/invoices", headers=_auth(token))
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1
    assert list_resp.json()["invoices"][0]["invoice_id"] == invoice_id

    get_resp = client.get(
        f"/v1/invoicing/orgs/{org_id}/invoices/{invoice_id}", headers=_auth(token)
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "draft"
    assert get_resp.json()["total_cents"] == 49050

    patch_resp = client.patch(
        f"/v1/invoicing/orgs/{org_id}/invoices/{invoice_id}",
        json={"customer_name": "New Name"},
        headers=_auth(token),
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["customer_name"] == "New Name"

    delete_resp = client.delete(
        f"/v1/invoicing/orgs/{org_id}/invoices/{invoice_id}", headers=_auth(token)
    )
    assert delete_resp.status_code == 204

    list_after_delete = client.get(f"/v1/invoicing/orgs/{org_id}/invoices", headers=_auth(token))
    assert list_after_delete.json()["total"] == 0


def test_create_invoice_requires_admin_role(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    owner_token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, owner_token, "Acme")

    member_token = make_token("auth0|member", "member@acme.com")
    _add_member(client, org_id, owner_token, "auth0|member", "member")

    resp = client.post(
        f"/v1/invoicing/orgs/{org_id}/invoices", json=_INVOICE_BODY, headers=_auth(member_token)
    )
    assert resp.status_code == 403


def test_member_can_read_invoices(client: TestClient, make_token: Callable[..., str]) -> None:
    owner_token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, owner_token, "Acme")
    invoice_id = _create_invoice(client, org_id, owner_token)

    member_token = make_token("auth0|member", "member@acme.com")
    _add_member(client, org_id, owner_token, "auth0|member", "member")

    resp = client.get(
        f"/v1/invoicing/orgs/{org_id}/invoices/{invoice_id}", headers=_auth(member_token)
    )
    assert resp.status_code == 200


# --- cross-org isolation ---


def test_get_invoice_cross_org_is_not_found(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    token_a = make_token("auth0|owner-a", "owner-a@acme.com")
    org_a = _create_org(client, token_a, "Acme")
    invoice_id = _create_invoice(client, org_a, token_a)

    token_b = make_token("auth0|owner-b", "owner-b@beta.com")
    org_b = _create_org(client, token_b, "Beta")

    resp = client.get(f"/v1/invoicing/orgs/{org_b}/invoices/{invoice_id}", headers=_auth(token_b))
    assert resp.status_code == 404


def test_non_member_cannot_create_invoice(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    owner_token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, owner_token, "Acme")

    stranger_token = make_token("auth0|stranger", "stranger@example.com")
    resp = client.post(
        f"/v1/invoicing/orgs/{org_id}/invoices", json=_INVOICE_BODY, headers=_auth(stranger_token)
    )
    assert resp.status_code == 404


# --- state transitions ---


def test_send_record_payment_void_flow(client: TestClient, make_token: Callable[..., str]) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, token, "Acme")
    invoice_id = _create_invoice(client, org_id, token)

    send_resp = client.post(
        f"/v1/invoicing/orgs/{org_id}/invoices/{invoice_id}/send",
        json={"recipient_email": "customer@example.com"},
        headers=_auth(token),
    )
    assert send_resp.status_code == 200
    assert send_resp.json()["status"] == "sent"
    assert send_resp.json()["pdf_s3_key"]

    resend_resp = client.post(
        f"/v1/invoicing/orgs/{org_id}/invoices/{invoice_id}/send",
        json={"recipient_email": "customer@example.com"},
        headers=_auth(token),
    )
    assert resend_resp.status_code == 409

    payment_resp = client.post(
        f"/v1/invoicing/orgs/{org_id}/invoices/{invoice_id}/record-payment",
        json={"amount_cents": 49050, "payment_date": "2026-07-30"},
        headers=_auth(token),
    )
    assert payment_resp.status_code == 200
    assert payment_resp.json()["status"] == "paid"
    assert payment_resp.json()["remaining_cents"] == 0

    payments_resp = client.get(
        f"/v1/invoicing/orgs/{org_id}/invoices/{invoice_id}/payments", headers=_auth(token)
    )
    assert payments_resp.status_code == 200
    assert len(payments_resp.json()["payments"]) == 1

    void_resp = client.post(
        f"/v1/invoicing/orgs/{org_id}/invoices/{invoice_id}/void",
        json={"reason": "refunded"},
        headers=_auth(token),
    )
    assert void_resp.status_code == 200
    assert void_resp.json()["status"] == "void"

    second_void_resp = client.post(
        f"/v1/invoicing/orgs/{org_id}/invoices/{invoice_id}/void",
        json={"reason": "again"},
        headers=_auth(token),
    )
    assert second_void_resp.status_code == 409


def test_record_payment_on_draft_rejected(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, token, "Acme")
    invoice_id = _create_invoice(client, org_id, token)

    resp = client.post(
        f"/v1/invoicing/orgs/{org_id}/invoices/{invoice_id}/record-payment",
        json={"amount_cents": 1000, "payment_date": "2026-07-25"},
        headers=_auth(token),
    )
    assert resp.status_code == 409


def test_list_invoices_status_filter(client: TestClient, make_token: Callable[..., str]) -> None:
    token = make_token("auth0|owner", "owner@acme.com")
    org_id = _create_org(client, token, "Acme")
    draft_id = _create_invoice(client, org_id, token)
    sent_id = _create_invoice(client, org_id, token)
    client.post(
        f"/v1/invoicing/orgs/{org_id}/invoices/{sent_id}/send",
        json={"recipient_email": "customer@example.com"},
        headers=_auth(token),
    )

    resp = client.get(
        f"/v1/invoicing/orgs/{org_id}/invoices", params={"status": "sent"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    ids = [inv["invoice_id"] for inv in resp.json()["invoices"]]
    assert ids == [sent_id]
    assert draft_id not in ids
