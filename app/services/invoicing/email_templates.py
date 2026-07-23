"""HTML/text email bodies for the invoice send flow (§9.1).

Kept separate from ``service.py`` so the templates are unit-testable as pure
string functions, with no DB or Core dependency.
"""

from __future__ import annotations

from app.core.settings import OrgSettings
from app.services.invoicing.models import Invoice


def _money(cents: int, currency_code: str) -> str:
    return f"{currency_code} {cents / 100:,.2f}"


def render_invoice_email_html(invoice: Invoice, *, org_settings: OrgSettings) -> str:
    sender_name = org_settings.sender_name or invoice.org_id
    due = f"<p>Due date: {invoice.due_date.isoformat()}</p>" if invoice.due_date else ""
    return (
        f"<p>Hi {invoice.customer_name},</p>"
        f"<p>{sender_name} has sent you invoice <strong>{invoice.invoice_number}</strong> "
        f"for <strong>{_money(invoice.total_cents, invoice.currency_code)}</strong>.</p>"
        f"{due}"
        f"<p>The invoice PDF is attached to this email.</p>"
        f"<p>Thank you for your business.</p>"
    )


def render_invoice_email_text(invoice: Invoice) -> str:
    due = f"\nDue date: {invoice.due_date.isoformat()}" if invoice.due_date else ""
    return (
        f"Hi {invoice.customer_name},\n\n"
        f"Invoice {invoice.invoice_number} for "
        f"{_money(invoice.total_cents, invoice.currency_code)} is attached to this email."
        f"{due}\n\n"
        f"Thank you for your business."
    )
