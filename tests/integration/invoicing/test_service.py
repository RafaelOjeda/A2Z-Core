"""Service-layer integration tests against real Postgres (§9, §11).

Membership is stubbed at ``access.get_membership`` (the single seam the
service layer's authz gate calls through), mirroring
``tests/integration/omnichannel/test_connections.py``. Read/mutation role
enforcement, the full lifecycle, and cross-org isolation are all covered
here at the service layer; ``test_router_http.py`` covers the HTTP wiring on
top of the same logic.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.membership import Membership, Role
from app.services.invoicing import access, service
from app.services.invoicing.exceptions import (
    InvalidStateTransitionError,
    InvoiceForbiddenError,
    InvoiceNotFoundError,
    InvoiceValidationError,
)
from app.services.invoicing.schemas import (
    InvoiceCreateRequest,
    InvoiceUpdateRequest,
    LineItemCreate,
    RecordPaymentRequest,
)

pytestmark = pytest.mark.integration


def _stub_membership(
    monkeypatch: pytest.MonkeyPatch, role: Role | None, org_id: str = "org-a"
) -> None:
    value = (
        None
        if role is None
        else Membership(
            user_id="user-1", org_id=org_id, role=role, joined_at=datetime.now(timezone.utc)
        )
    )
    monkeypatch.setattr(access, "get_membership", AsyncMock(return_value=value))


def _create_body(**overrides: object) -> InvoiceCreateRequest:
    defaults: dict[str, object] = dict(
        customer_email="jane@example.com",
        customer_name="Jane Smith",
        customer_company="Acme Co",
        invoice_date=date(2026, 7, 22),
        due_date=date(2026, 8, 21),
        payment_terms="net-30",
        line_items=[LineItemCreate(description="Consulting", quantity=3, unit_price_cents=15_000)],
        tax_cents=4_050,
        discount_cents=0,
        notes="Thanks!",
    )
    defaults.update(overrides)
    return InvoiceCreateRequest(**defaults)  # type: ignore[arg-type]


# --- create_invoice ---


async def test_create_invoice_as_admin(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)

    invoice, line_items = await service.create_invoice(session, "org-a", "user-1", _create_body())

    assert invoice.status == "draft"
    assert invoice.org_id == "org-a"
    assert invoice.invoice_number.startswith("INV-")
    assert invoice.subtotal_cents == 45_000
    assert invoice.total_cents == 49_050
    assert len(line_items) == 1
    assert line_items[0].amount_cents == 45_000


async def test_create_invoice_number_increments(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.OWNER)

    first, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    second, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())

    assert first.invoice_number != second.invoice_number


async def test_create_invoice_requires_mutation_role(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.MEMBER)

    with pytest.raises(InvoiceForbiddenError):
        await service.create_invoice(session, "org-a", "user-1", _create_body())


async def test_create_invoice_requires_membership(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, None)

    with pytest.raises(NotFoundError):
        await service.create_invoice(session, "org-a", "user-1", _create_body())


async def test_create_invoice_rejects_negative_total(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)

    with pytest.raises(InvoiceValidationError):
        await service.create_invoice(
            session, "org-a", "user-1", _create_body(tax_cents=0, discount_cents=999_999)
        )


# --- get_invoice / list_invoices / cross-org isolation ---


async def test_get_invoice_round_trip(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    created, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())

    invoice, line_items = await service.get_invoice(session, "org-a", "user-1", created.id)

    assert invoice.id == created.id
    assert len(line_items) == 1


async def test_get_invoice_cross_org_is_not_found(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN, org_id="org-a")
    created, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())

    _stub_membership(monkeypatch, Role.ADMIN, org_id="org-b")
    with pytest.raises(InvoiceNotFoundError):
        await service.get_invoice(session, "org-b", "user-1", created.id)


async def test_list_invoices_scoped_to_org(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN, org_id="org-a")
    await service.create_invoice(session, "org-a", "user-1", _create_body())

    _stub_membership(monkeypatch, Role.ADMIN, org_id="org-b")
    await service.create_invoice(session, "org-b", "user-1", _create_body())

    _stub_membership(monkeypatch, Role.MEMBER, org_id="org-a")
    rows, total = await service.list_invoices(session, "org-a", "user-1")

    assert total == 1
    assert len(rows) == 1
    assert rows[0][0].org_id == "org-a"


async def test_list_invoices_filters_by_status(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    draft, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    sent, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    await service.send_invoice(session, "org-a", sent.id, "user-1", "customer@example.com")

    rows, total = await service.list_invoices(session, "org-a", "user-1", statuses=["sent"])

    assert total == 1
    assert rows[0][0].id == sent.id


async def test_list_invoices_excludes_soft_deleted(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    await service.soft_delete_invoice(session, "org-a", invoice.id, "user-1")

    rows, total = await service.list_invoices(session, "org-a", "user-1")

    assert total == 0
    assert rows == []


# --- update_invoice ---


async def test_update_invoice_edits_fields(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())

    updated, _ = await service.update_invoice(
        session, "org-a", invoice.id, "user-1", InvoiceUpdateRequest(customer_name="New Name")
    )

    assert updated.customer_name == "New Name"


async def test_update_invoice_recomputes_totals_on_line_item_change(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())

    updated, line_items = await service.update_invoice(
        session,
        "org-a",
        invoice.id,
        "user-1",
        InvoiceUpdateRequest(
            line_items=[LineItemCreate(description="New item", quantity=1, unit_price_cents=10_000)]
        ),
    )

    assert len(line_items) == 1
    assert updated.subtotal_cents == 10_000
    assert updated.total_cents == 10_000 + 4_050  # tax unchanged from creation


async def test_update_invoice_rejects_void(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    await service.void_invoice(session, "org-a", invoice.id, "user-1", "no longer needed")

    with pytest.raises(InvalidStateTransitionError):
        await service.update_invoice(
            session, "org-a", invoice.id, "user-1", InvoiceUpdateRequest(notes="edit attempt")
        )


# --- soft_delete_invoice ---


async def test_soft_delete_hides_from_get(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())

    await service.soft_delete_invoice(session, "org-a", invoice.id, "user-1")

    with pytest.raises(InvoiceNotFoundError):
        await service.get_invoice(session, "org-a", "user-1", invoice.id)


async def test_soft_delete_requires_mutation_role(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())

    _stub_membership(monkeypatch, Role.MEMBER)
    with pytest.raises(InvoiceForbiddenError):
        await service.soft_delete_invoice(session, "org-a", invoice.id, "user-1")


# --- send_invoice ---


async def test_send_invoice_transitions_to_sent(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())

    sent, email_result = await service.send_invoice(
        session, "org-a", invoice.id, "user-1", "customer@example.com"
    )

    assert sent.status == "sent"
    assert sent.pdf_s3_key is not None
    assert email_result.status.value == "sent"


async def test_send_invoice_rejects_already_sent(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    await service.send_invoice(session, "org-a", invoice.id, "user-1", "customer@example.com")

    with pytest.raises(InvalidStateTransitionError):
        await service.send_invoice(session, "org-a", invoice.id, "user-1", "customer@example.com")


async def test_send_invoice_generates_signed_pdf_url(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    sent, _ = await service.send_invoice(
        session, "org-a", invoice.id, "user-1", "customer@example.com"
    )

    url = service.signed_pdf_url(sent)

    assert url is not None
    assert sent.pdf_s3_key in url


def test_signed_pdf_url_none_before_send() -> None:
    from app.services.invoicing.models import Invoice

    invoice = Invoice(pdf_s3_key=None)
    assert service.signed_pdf_url(invoice) is None


# --- record_payment ---


async def test_record_payment_partial_then_full(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    await service.send_invoice(session, "org-a", invoice.id, "user-1", "customer@example.com")

    partial, _ = await service.record_payment(
        session,
        "org-a",
        invoice.id,
        "user-1",
        RecordPaymentRequest(amount_cents=20_000, payment_date=date(2026, 7, 25)),
    )
    assert partial.status == "partially_paid"
    assert partial.payment_status == "partially_paid"

    full, _ = await service.record_payment(
        session,
        "org-a",
        invoice.id,
        "user-1",
        RecordPaymentRequest(amount_cents=29_050, payment_date=date(2026, 7, 26)),
    )
    assert full.status == "paid"
    assert full.payment_status == "paid"
    assert full.amount_paid_cents == 49_050


async def test_record_payment_rejects_on_draft(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())

    with pytest.raises(InvalidStateTransitionError):
        await service.record_payment(
            session,
            "org-a",
            invoice.id,
            "user-1",
            RecordPaymentRequest(amount_cents=1_000, payment_date=date(2026, 7, 25)),
        )


async def test_record_payment_idempotency_key_replay(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    await service.send_invoice(session, "org-a", invoice.id, "user-1", "customer@example.com")

    body = RecordPaymentRequest(
        amount_cents=10_000, payment_date=date(2026, 7, 25), idempotency_key="idem-1"
    )
    first_invoice, first_payment = await service.record_payment(
        session, "org-a", invoice.id, "user-1", body
    )
    second_invoice, second_payment = await service.record_payment(
        session, "org-a", invoice.id, "user-1", body
    )

    assert first_payment.id == second_payment.id
    assert second_invoice.amount_paid_cents == 10_000  # not double-counted


# --- void_invoice ---


async def test_void_from_draft(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())

    voided = await service.void_invoice(session, "org-a", invoice.id, "user-1", "customer canceled")

    assert voided.status == "void"


async def test_void_twice_rejected(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    await service.void_invoice(session, "org-a", invoice.id, "user-1", "first void")

    with pytest.raises(InvalidStateTransitionError):
        await service.void_invoice(session, "org-a", invoice.id, "user-1", "second void")


# --- list_payments ---


async def test_list_payments_cross_org_isolation(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN, org_id="org-a")
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    await service.send_invoice(session, "org-a", invoice.id, "user-1", "customer@example.com")
    await service.record_payment(
        session,
        "org-a",
        invoice.id,
        "user-1",
        RecordPaymentRequest(amount_cents=5_000, payment_date=date(2026, 7, 25)),
    )

    _stub_membership(monkeypatch, Role.ADMIN, org_id="org-b")
    with pytest.raises(InvoiceNotFoundError):
        await service.list_payments(session, "org-b", "user-1", invoice.id)


# --- full lifecycle ---


async def test_full_lifecycle_via_send_pay_states(
    aws: None, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_membership(monkeypatch, Role.ADMIN)
    invoice, _ = await service.create_invoice(session, "org-a", "user-1", _create_body())
    assert invoice.status == "draft"

    sent, _ = await service.send_invoice(
        session, "org-a", invoice.id, "user-1", "customer@example.com"
    )
    assert sent.status == "sent"

    # Editable even after send (§3.1: "fully editable even after send").
    edited, _ = await service.update_invoice(
        session, "org-a", invoice.id, "user-1", InvoiceUpdateRequest(notes="updated after send")
    )
    assert edited.notes == "updated after send"
    assert edited.status == "sent"  # status itself never moves backwards

    paid, _ = await service.record_payment(
        session,
        "org-a",
        invoice.id,
        "user-1",
        RecordPaymentRequest(amount_cents=49_050, payment_date=date(2026, 7, 30)),
    )
    assert paid.status == "paid"

    voided = await service.void_invoice(session, "org-a", invoice.id, "user-1", "refunded")
    assert voided.status == "void"
