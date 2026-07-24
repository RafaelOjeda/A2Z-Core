"""Request/response Pydantic models for the Invoicing HTTP surface (§9).

Plain ``str`` for email addresses (not ``EmailStr``) to match the rest of the
repo's convention -- ``core.email.send_email`` does its own loose validation
and raises ``InvalidAddressError``; Invoicing doesn't duplicate that check.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.services.invoicing.models import Invoice, InvoiceLineItem, InvoicePayment


class LineItemCreate(BaseModel):
    description: str
    quantity: Decimal = Field(gt=0)
    unit_price_cents: int = Field(ge=0)


class LineItemResponse(BaseModel):
    line_item_id: str
    description: str
    quantity: Decimal
    unit_price_cents: int
    amount_cents: int

    @classmethod
    def from_model(cls, item: InvoiceLineItem) -> LineItemResponse:
        return cls(
            line_item_id=item.id,
            description=item.description,
            quantity=item.quantity,
            unit_price_cents=item.unit_price_cents,
            amount_cents=item.amount_cents,
        )


class InvoiceCreateRequest(BaseModel):
    customer_email: str
    customer_name: str
    customer_company: str | None = None
    invoice_date: date
    due_date: date | None = None
    payment_terms: str | None = None
    line_items: list[LineItemCreate] = Field(min_length=1)
    tax_cents: int = Field(default=0, ge=0)
    discount_cents: int = Field(default=0, ge=0)
    notes: str | None = None


class InvoiceUpdateRequest(BaseModel):
    """All fields optional -- PATCH applies only what's provided. Allowed on any
    non-``void`` invoice (§9); totals are recomputed if ``line_items``,
    ``tax_cents``, or ``discount_cents`` change."""

    customer_email: str | None = None
    customer_name: str | None = None
    customer_company: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    payment_terms: str | None = None
    line_items: list[LineItemCreate] | None = None
    tax_cents: int | None = Field(default=None, ge=0)
    discount_cents: int | None = Field(default=None, ge=0)
    notes: str | None = None


class InvoiceDetail(BaseModel):
    """Full response model (§9), including line items and a 1-hour signed PDF
    URL when a PDF exists."""

    org_id: str
    invoice_id: str
    invoice_number: str
    status: str

    customer_email: str
    customer_name: str
    customer_company: str | None

    invoice_date: date
    due_date: date | None
    payment_terms: str | None

    line_items: list[LineItemResponse]

    subtotal_cents: int
    tax_cents: int
    discount_cents: int
    total_cents: int

    amount_paid_cents: int
    payment_status: str
    remaining_cents: int

    currency_code: str
    notes: str | None

    pdf_s3_key: str | None
    pdf_signed_url: str | None = None

    created_by: str
    created_at: datetime
    updated_at: datetime

    is_deleted: bool

    @classmethod
    def from_model(
        cls,
        invoice: Invoice,
        line_items: list[InvoiceLineItem],
        *,
        pdf_signed_url: str | None = None,
    ) -> InvoiceDetail:
        return cls(
            org_id=invoice.org_id,
            invoice_id=invoice.id,
            invoice_number=invoice.invoice_number,
            status=invoice.status,
            customer_email=invoice.customer_email,
            customer_name=invoice.customer_name,
            customer_company=invoice.customer_company,
            invoice_date=invoice.invoice_date,
            due_date=invoice.due_date,
            payment_terms=invoice.payment_terms,
            line_items=[LineItemResponse.from_model(i) for i in line_items],
            subtotal_cents=invoice.subtotal_cents,
            tax_cents=invoice.tax_cents,
            discount_cents=invoice.discount_cents,
            total_cents=invoice.total_cents,
            amount_paid_cents=invoice.amount_paid_cents,
            payment_status=invoice.payment_status,
            remaining_cents=max(invoice.total_cents - invoice.amount_paid_cents, 0),
            currency_code=invoice.currency_code,
            notes=invoice.notes,
            pdf_s3_key=invoice.pdf_s3_key,
            pdf_signed_url=pdf_signed_url,
            created_by=invoice.created_by,
            created_at=invoice.created_at,
            updated_at=invoice.updated_at,
            is_deleted=invoice.is_deleted,
        )


class InvoiceListResponse(BaseModel):
    invoices: list[InvoiceDetail]
    total: int


class SendInvoiceRequest(BaseModel):
    recipient_email: str


class SendInvoiceResponse(BaseModel):
    invoice_id: str
    status: str
    pdf_s3_key: str
    sent_at: datetime


class RecordPaymentRequest(BaseModel):
    amount_cents: int = Field(gt=0)
    payment_date: date
    payment_method: str = "manual"
    reference: str | None = None
    notes: str | None = None
    idempotency_key: str | None = None


class RecordPaymentResponse(BaseModel):
    invoice_id: str
    status: str
    amount_paid_cents: int
    payment_status: str
    remaining_cents: int
    payment_id: str


class VoidRequest(BaseModel):
    reason: str


class VoidResponse(BaseModel):
    invoice_id: str
    status: str
    voided_at: datetime


class PaymentResponse(BaseModel):
    payment_id: str
    amount_cents: int
    payment_date: date
    payment_method: str
    reference: str | None
    notes: str | None
    recorded_by: str
    recorded_at: datetime

    @classmethod
    def from_model(cls, payment: InvoicePayment) -> PaymentResponse:
        return cls(
            payment_id=payment.id,
            amount_cents=payment.amount_cents,
            payment_date=payment.payment_date,
            payment_method=payment.payment_method,
            reference=payment.reference,
            notes=payment.notes,
            recorded_by=payment.recorded_by,
            recorded_at=payment.recorded_at,
        )


class PaymentListResponse(BaseModel):
    payments: list[PaymentResponse]
