"""Unit tests for the send-flow email bodies (§9.1) -- pure string functions."""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.core.settings import OrgSettings
from app.services.invoicing.email_templates import (
    render_invoice_email_html,
    render_invoice_email_text,
)
from app.services.invoicing.models import Invoice


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
        notes=None,
        pdf_s3_key=None,
        is_deleted=False,
        created_by="user-1",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Invoice(**defaults)


def test_html_includes_invoice_number_and_total() -> None:
    invoice = _invoice()
    org_settings = OrgSettings(org_id="org-a", sender_name="Acme Consulting")

    html = render_invoice_email_html(invoice, org_settings=org_settings)

    assert "INV-2026-000001" in html
    assert "USD 490.50" in html
    assert "Acme Consulting" in html
    assert "2026-08-21" in html


def test_html_falls_back_to_org_id_without_sender_name() -> None:
    invoice = _invoice()
    org_settings = OrgSettings(org_id="org-a", sender_name="")

    html = render_invoice_email_html(invoice, org_settings=org_settings)

    assert "org-a" in html


def test_html_omits_due_date_when_absent() -> None:
    invoice = _invoice(due_date=None)
    org_settings = OrgSettings(org_id="org-a", sender_name="Acme")

    html = render_invoice_email_html(invoice, org_settings=org_settings)

    assert "Due date" not in html


def test_text_includes_invoice_number_and_total() -> None:
    invoice = _invoice()

    text = render_invoice_email_text(invoice)

    assert "INV-2026-000001" in text
    assert "USD 490.50" in text
