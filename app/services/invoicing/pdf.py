"""Invoice PDF rendering (§9.1, §14).

**Open decision resolved:** ``reportlab`` was chosen over ``weasyprint`` for
the new dependency (§14 of the design doc) -- pure Python, no system
libraries (cairo/pango) required, which keeps the Docker image and CI
runners simple. A fresh PDF is rendered on every ``send`` (§3.1's decision),
never cached across sends.

The org's customer-facing display name comes from
``OrgSettings.sender_name`` (already used for the email "From" display name
per root CLAUDE.md §8) -- not a new Core field.
"""

from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.settings import OrgSettings
from app.services.invoicing.models import Invoice, InvoiceLineItem


def _money(cents: int, currency_code: str) -> str:
    return f"{currency_code} {cents / 100:,.2f}"


def render_invoice_pdf(
    invoice: Invoice,
    line_items: list[InvoiceLineItem],
    *,
    org_settings: OrgSettings,
) -> bytes:
    """Render an invoice to a PDF and return its bytes.

    Performance target: comparable to Core's storage/email budgets -- this is
    a synchronous, in-memory render (no external process), typically well
    under 500ms for a normal-sized invoice.
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("InvoiceTitle", parent=styles["Title"], alignment=0)
    normal = styles["Normal"]

    sender_name = org_settings.sender_name or invoice.org_id
    elements = [
        Paragraph(sender_name, title_style),
        Paragraph(f"Invoice {invoice.invoice_number}", styles["Heading2"]),
        Spacer(1, 12),
        Paragraph(f"Invoice date: {invoice.invoice_date.isoformat()}", normal),
    ]
    if invoice.due_date:
        elements.append(Paragraph(f"Due date: {invoice.due_date.isoformat()}", normal))
    if invoice.payment_terms:
        elements.append(Paragraph(f"Terms: {invoice.payment_terms}", normal))
    elements.append(Spacer(1, 12))

    bill_to_lines = [invoice.customer_name]
    if invoice.customer_company:
        bill_to_lines.append(invoice.customer_company)
    bill_to_lines.append(invoice.customer_email)
    elements.append(Paragraph("Bill to:", styles["Heading4"]))
    for line in bill_to_lines:
        elements.append(Paragraph(line, normal))
    elements.append(Spacer(1, 16))

    table_data = [["Description", "Qty", "Unit price", "Amount"]]
    for item in line_items:
        table_data.append(
            [
                item.description,
                str(item.quantity),
                _money(item.unit_price_cents, invoice.currency_code),
                _money(item.amount_cents, invoice.currency_code),
            ]
        )
    items_table = Table(table_data, colWidths=[3.2 * inch, 0.8 * inch, 1.2 * inch, 1.2 * inch])
    items_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ]
        )
    )
    elements.append(items_table)
    elements.append(Spacer(1, 16))

    totals_data = [
        ["Subtotal", _money(invoice.subtotal_cents, invoice.currency_code)],
        ["Tax", _money(invoice.tax_cents, invoice.currency_code)],
        ["Discount", f"-{_money(invoice.discount_cents, invoice.currency_code)}"],
        ["Total", _money(invoice.total_cents, invoice.currency_code)],
    ]
    totals_table = Table(totals_data, colWidths=[4.4 * inch, 1.2 * inch])
    totals_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.black),
            ]
        )
    )
    elements.append(totals_table)

    if invoice.notes:
        elements.append(Spacer(1, 20))
        elements.append(Paragraph("Notes", styles["Heading4"]))
        elements.append(Paragraph(invoice.notes, normal))

    doc.build(elements)
    return buffer.getvalue()
