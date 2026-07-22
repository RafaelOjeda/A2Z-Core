"""PDF generation for invoices.

Renders an invoice as HTML, converts to PDF, and uploads to S3.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

from app.core.storage import upload_file
from app.services.invoicing.models import InvoiceRead
from app.services.invoicing.exceptions import PDFGenerationError


async def generate_and_upload_invoice_pdf(
    org_id: str,
    invoice: InvoiceRead,
) -> str:
    """Generate a PDF for an invoice and upload it to S3.

    Args:
        org_id: The org owner.
        invoice: The invoice to render.

    Returns:
        The S3 key where the PDF was stored.

    Raises:
        PDFGenerationError: If PDF generation fails.
    """
    try:
        import weasyprint
    except ImportError:
        raise PDFGenerationError("weasyprint not installed; cannot generate PDF")

    # Render invoice as HTML
    html_content = _render_invoice_html(invoice)

    # Convert HTML to PDF
    try:
        pdf_bytes = weasyprint.HTML(string=html_content).write_pdf()
    except Exception as e:
        raise PDFGenerationError(f"PDF generation failed: {e}")

    # Upload to S3 via core.storage
    file_key = f"{org_id}/invoicing/{invoice.invoice_id}.pdf"
    pdf_bytes_io = BytesIO(pdf_bytes)

    try:
        result = await upload_file(
            org_id=org_id,
            service_type="invoicing",
            file_key=file_key,
            file_content=pdf_bytes_io,
            content_type="application/pdf",
            ttl_days=365,  # 1-year retention per docs/retention.md
        )
        return result.key
    except Exception as e:
        raise PDFGenerationError(f"Failed to upload PDF to S3: {e}")


def _render_invoice_html(invoice: InvoiceRead) -> str:
    """Render an invoice as HTML suitable for PDF conversion.

    Simple template; can be enhanced with CSS for branding later.
    """
    line_items_html = ""
    for item in invoice.line_items:
        line_items_html += f"""
        <tr>
            <td>{item.description}</td>
            <td style="text-align: right;">{item.quantity}</td>
            <td style="text-align: right;">${item.unit_price_cents / 100:.2f}</td>
            <td style="text-align: right;">${item.amount_cents / 100:.2f}</td>
        </tr>
        """

    subtotal = invoice.subtotal_cents / 100
    tax = invoice.tax_cents / 100
    discount = invoice.discount_cents / 100
    total = invoice.total_cents / 100
    paid = invoice.paid_cents / 100
    balance = total - paid

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Invoice {invoice.invoice_number}</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 40px;
                color: #333;
            }}
            .header {{
                margin-bottom: 40px;
            }}
            .invoice-number {{
                font-size: 24px;
                font-weight: bold;
                color: #2c3e50;
            }}
            .invoice-date {{
                font-size: 12px;
                color: #7f8c8d;
                margin-top: 5px;
            }}
            .customer-info {{
                margin-top: 30px;
                margin-bottom: 30px;
            }}
            .customer-name {{
                font-weight: bold;
                font-size: 14px;
            }}
            .customer-detail {{
                font-size: 12px;
                color: #7f8c8d;
                margin: 3px 0;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 20px;
                margin-bottom: 20px;
            }}
            table th {{
                background-color: #ecf0f1;
                padding: 10px;
                text-align: left;
                border-bottom: 2px solid #bdc3c7;
                font-weight: bold;
            }}
            table td {{
                padding: 10px;
                border-bottom: 1px solid #ecf0f1;
            }}
            .totals {{
                margin-left: auto;
                width: 300px;
                margin-top: 20px;
            }}
            .totals-row {{
                display: flex;
                justify-content: space-between;
                padding: 8px 0;
                font-size: 12px;
            }}
            .totals-row.total {{
                border-top: 2px solid #2c3e50;
                border-bottom: 2px solid #2c3e50;
                font-weight: bold;
                font-size: 14px;
                padding: 10px 0;
            }}
            .amount {{
                text-align: right;
                font-weight: bold;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <div class="invoice-number">Invoice {invoice.invoice_number}</div>
            <div class="invoice-date">Date: {invoice.invoice_date.strftime('%B %d, %Y')}</div>
            <div class="invoice-date">Due: {invoice.due_date.strftime('%B %d, %Y')}</div>
        </div>

        <div class="customer-info">
            <div class="customer-name">{invoice.customer_name}</div>
            {f'<div class="customer-detail">{invoice.customer_company}</div>' if invoice.customer_company else ''}
            <div class="customer-detail">{invoice.customer_email}</div>
            {f'<div class="customer-detail">Terms: {invoice.payment_terms}</div>' if invoice.payment_terms else ''}
        </div>

        <table>
            <thead>
                <tr>
                    <th>Description</th>
                    <th style="text-align: right;">Quantity</th>
                    <th style="text-align: right;">Unit Price</th>
                    <th style="text-align: right;">Amount</th>
                </tr>
            </thead>
            <tbody>
                {line_items_html}
            </tbody>
        </table>

        <div class="totals">
            <div class="totals-row">
                <span>Subtotal:</span>
                <span class="amount">${subtotal:.2f}</span>
            </div>
            {f'<div class="totals-row"><span>Tax:</span><span class="amount">${tax:.2f}</span></div>' if tax > 0 else ''}
            {f'<div class="totals-row"><span>Discount:</span><span class="amount">-${discount:.2f}</span></div>' if discount > 0 else ''}
            <div class="totals-row total">
                <span>Total Due:</span>
                <span class="amount">${total:.2f}</span>
            </div>
            {f'<div class="totals-row"><span>Paid:</span><span class="amount">${paid:.2f}</span></div>' if paid > 0 else ''}
            {f'<div class="totals-row"><span>Balance:</span><span class="amount">${balance:.2f}</span></div>' if balance > 0 else ''}
        </div>

        {f'<div style="margin-top: 40px; font-size: 12px; color: #7f8c8d;"><strong>Notes:</strong><br/>{invoice.notes}</div>' if invoice.notes else ''}
    </body>
    </html>
    """
