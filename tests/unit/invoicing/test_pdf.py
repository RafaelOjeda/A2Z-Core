"""Unit tests for PDF rendering (§9.1) -- no DB, no I/O, no AWS."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from app.core.settings import OrgSettings
from app.services.invoicing.models import Invoice, InvoiceLineItem
from app.services.invoicing.pdf import render_invoice_pdf


def _invoice(**overrides: object) -> Invoice:
    defaults: dict[str, object] = dict(
        id="inv-1",
        org_id="org-a",
        invoice_number="INV-2026-000001",
        status="draft",
        customer_email="jane@example.com",
        customer_name="Jane Smith",
        customer_company="Acme Co",
        invoice_date=date(2026, 7, 22),
        due_date=date(2026, 8, 21),
        payment_terms="net-30",
        subtotal_cents=45_000,
        tax_cents=4_050,
        discount_cents=0,
        total_cents=49_050,
        amount_paid_cents=0,
        payment_status="unpaid",
        currency_code="USD",
        notes="Thank you for your business!",
        pdf_s3_key=None,
        is_deleted=False,
        created_by="user-1",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Invoice(**defaults)


def _line_item(**overrides: object) -> InvoiceLineItem:
    defaults: dict[str, object] = dict(
        id="li-1",
        org_id="org-a",
        invoice_id="inv-1",
        description="Consulting - 3 hours",
        quantity=Decimal("3"),
        unit_price_cents=15_000,
        amount_cents=45_000,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return InvoiceLineItem(**defaults)


def test_render_invoice_pdf_produces_valid_pdf_bytes() -> None:
    invoice = _invoice()
    line_items = [_line_item()]
    org_settings = OrgSettings(org_id="org-a", sender_name="Acme Consulting")

    pdf_bytes = render_invoice_pdf(invoice, line_items, org_settings=org_settings)

    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 0


def test_render_invoice_pdf_falls_back_to_org_id_without_sender_name() -> None:
    invoice = _invoice()
    org_settings = OrgSettings(org_id="org-a", sender_name="")

    pdf_bytes = render_invoice_pdf(invoice, [], org_settings=org_settings)

    assert pdf_bytes.startswith(b"%PDF-")


def test_render_invoice_pdf_handles_multiple_line_items() -> None:
    invoice = _invoice()
    line_items = [
        _line_item(
            description="Item A", quantity=Decimal("1"), unit_price_cents=1_000, amount_cents=1_000
        ),
        _line_item(
            description="Item B",
            quantity=Decimal("2.5"),
            unit_price_cents=2_000,
            amount_cents=5_000,
        ),
    ]
    org_settings = OrgSettings(org_id="org-a", sender_name="Acme")

    pdf_bytes = render_invoice_pdf(invoice, line_items, org_settings=org_settings)

    assert pdf_bytes.startswith(b"%PDF-")


def test_render_invoice_pdf_without_due_date_or_notes() -> None:
    invoice = _invoice(due_date=None, payment_terms=None, notes=None)
    org_settings = OrgSettings(org_id="org-a", sender_name="Acme")

    pdf_bytes = render_invoice_pdf(invoice, [], org_settings=org_settings)

    assert pdf_bytes.startswith(b"%PDF-")
