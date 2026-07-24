"""SQLAlchemy ORM models for the invoicing schema.

All tables carry org_id as the first column and every query filters on it
(golden rule #2). Money is stored as integer cents, never floats.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import (
    DATE,
    DECIMAL,
    TIMESTAMP,
    BigInteger,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all Invoicing ORM models."""

    pass


class Invoice(Base):
    """An invoice: the billable document with line items and payments."""

    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("org_id", "invoice_number", name="uq_org_invoice_number"),)

    org_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID

    invoice_number: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50))  # draft, sent, partially_paid, paid, void

    customer_name: Mapped[str] = mapped_column(String(255))
    customer_email: Mapped[str] = mapped_column(String(255))
    customer_company: Mapped[str] = mapped_column(String(255), nullable=True)

    invoice_date: Mapped[date] = mapped_column(DATE)
    due_date: Mapped[date] = mapped_column(DATE)
    payment_terms: Mapped[str] = mapped_column(String(255), nullable=True)

    subtotal_cents: Mapped[int] = mapped_column(BigInteger)
    tax_cents: Mapped[int] = mapped_column(BigInteger)
    discount_cents: Mapped[int] = mapped_column(BigInteger)
    total_cents: Mapped[int] = mapped_column(BigInteger)

    paid_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    notes: Mapped[str] = mapped_column(Text, nullable=True)

    pdf_key: Mapped[str] = mapped_column(String(500), nullable=True)  # S3 key
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    void_reason: Mapped[str] = mapped_column(String(500), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    line_items: Mapped[list[InvoiceLineItem]] = relationship(
        "InvoiceLineItem", back_populates="invoice", cascade="all, delete-orphan"
    )
    payments: Mapped[list[InvoicePayment]] = relationship(
        "InvoicePayment", back_populates="invoice", cascade="all, delete-orphan"
    )


class InvoiceLineItem(Base):
    """One billable row on an invoice."""

    __tablename__ = "invoice_line_items"

    org_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    line_item_id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID

    invoice_id: Mapped[str] = mapped_column(String(36), ForeignKey("invoices.invoice_id"))

    description: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[Decimal] = mapped_column(DECIMAL(15, 4))
    unit_price_cents: Mapped[int] = mapped_column(BigInteger)
    amount_cents: Mapped[int] = mapped_column(BigInteger)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())

    # Relationship
    invoice: Mapped[Invoice] = relationship("Invoice", back_populates="line_items")


class InvoicePayment(Base):
    """One recorded receipt against an invoice."""

    __tablename__ = "invoice_payments"

    org_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID

    invoice_id: Mapped[str] = mapped_column(String(36), ForeignKey("invoices.invoice_id"))

    amount_cents: Mapped[int] = mapped_column(BigInteger)
    payment_date: Mapped[date] = mapped_column(DATE)
    method: Mapped[str] = mapped_column(String(50))  # check, ach, credit_card, cash, other
    reference: Mapped[str] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())

    # Relationship
    invoice: Mapped[Invoice] = relationship("Invoice", back_populates="payments")


# Pydantic DTOs (for API requests/responses)


class LineItemCreate(BaseModel):
    """Input for creating a line item."""

    description: str = Field(..., min_length=1, max_length=500)
    quantity: Decimal = Field(..., gt=0, decimal_places=4)
    unit_price_cents: int = Field(..., gt=0)


class LineItemRead(BaseModel):
    """Output when reading a line item."""

    line_item_id: str
    description: str
    quantity: Decimal
    unit_price_cents: int
    amount_cents: int


class InvoiceCreate(BaseModel):
    """Input for creating an invoice."""

    customer_name: str = Field(..., min_length=1, max_length=255)
    customer_email: str = Field(..., min_length=1, max_length=255)
    customer_company: str | None = Field(None, max_length=255)

    invoice_date: date
    due_date: date
    payment_terms: str | None = Field(None, max_length=255)

    tax_cents: int = Field(default=0, ge=0)
    discount_cents: int = Field(default=0, ge=0)
    notes: str | None = None

    line_items: list[LineItemCreate] = Field(..., min_items=1)


class InvoiceUpdate(BaseModel):
    """Input for updating an invoice (draft only)."""

    customer_name: str | None = Field(None, min_length=1, max_length=255)
    customer_email: str | None = Field(None, min_length=1, max_length=255)
    customer_company: str | None = Field(None, max_length=255)

    invoice_date: date | None = None
    due_date: date | None = None
    payment_terms: str | None = Field(None, max_length=255)

    tax_cents: int | None = Field(None, ge=0)
    discount_cents: int | None = Field(None, ge=0)
    notes: str | None = None

    line_items: list[LineItemCreate] | None = None


class PaymentCreate(BaseModel):
    """Input for recording a payment."""

    amount_cents: int = Field(..., gt=0)
    payment_date: date
    method: str = Field(..., min_length=1, max_length=50)
    reference: str | None = Field(None, max_length=255)


class InvoiceRead(BaseModel):
    """Output when reading an invoice."""

    invoice_id: str
    invoice_number: str
    status: str
    customer_name: str
    customer_email: str
    customer_company: str | None

    invoice_date: date
    due_date: date
    payment_terms: str | None

    subtotal_cents: int
    tax_cents: int
    discount_cents: int
    total_cents: int
    paid_cents: int

    notes: str | None
    pdf_key: str | None
    sent_at: datetime | None
    void_reason: str | None
    voided_at: datetime | None

    line_items: list[LineItemRead]
    created_at: datetime
    updated_at: datetime
