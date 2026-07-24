"""Invoicing's Postgres data model (app/services/invoicing/CLAUDE.md §7).

Three tables in the ``invoicing`` schema on the shared Postgres instance,
every table ``org_id``-scoped first (root golden rule #2). Money is stored as
integer cents (``BigInteger``) -- never floats -- per the design's explicit
decision. Status columns are ``Text``, never a Postgres ``ENUM``: adding a new
status or payment method must never require a schema migration, mirroring
Omni-Channel's ``channel_type``/``status`` convention (see
``app/services/omnichannel/models.py``).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(schema="invoicing")


def _uuid() -> str:
    return str(uuid.uuid4())


class InvoiceStatus(str, Enum):
    """Invoice lifecycle status (§3.1). Linear, no backtracking; ``void`` is
    terminal and reachable from any other state."""

    DRAFT = "draft"
    SENT = "sent"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"


class PaymentStatus(str, Enum):
    """Denormalized payment-progress mirror of ``InvoiceStatus`` (§7), kept in
    sync on every ``record_payment`` call so "who owes me" queries don't need
    to interpret the full lifecycle status."""

    UNPAID = "unpaid"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"


class Invoice(Base):
    """An invoice header (§7). Owns many line items and many payments."""

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("org_id", "invoice_number", name="uq_invoice_number"),
        Index("ix_invoices_org_created", "org_id", "created_at"),
        Index("ix_invoices_org_status", "org_id", "status"),
        Index("ix_invoices_org_due_date", "org_id", "due_date"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    invoice_number: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=InvoiceStatus.DRAFT.value)

    # Customer details -- stored inline in v1 (no separate customer entity, §15).
    customer_email: Mapped[str] = mapped_column(Text, nullable=False)
    customer_name: Mapped[str] = mapped_column(Text, nullable=False)
    customer_company: Mapped[str | None] = mapped_column(Text, nullable=True)

    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Money as integer cents -- never floats (§7).
    subtotal_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tax_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    amount_paid_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    payment_status: Mapped[str] = mapped_column(
        Text, nullable=False, default=PaymentStatus.UNPAID.value
    )

    # Org default in v1 -- per-invoice currency override is Phase 3 (§15).
    currency_code: Mapped[str] = mapped_column(Text, nullable=False, default="USD")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    pdf_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InvoiceLineItem(Base):
    """One billable row on an invoice (§7)."""

    __tablename__ = "invoice_line_items"
    __table_args__ = (Index("ix_line_items_org_invoice", "org_id", "invoice_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    invoice_id: Mapped[str] = mapped_column(
        String, ForeignKey("invoicing.invoices.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Denormalized: quantity * unit_price_cents, rounded to the nearest cent.
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvoicePayment(Base):
    """A payment recorded against an invoice (§7). Invoice-level only in v1
    (no line-item attribution, §15). ``payment_method``/``idempotency_key``
    make this webhook-ready for Phase 3 Stripe/PayPal without a migration."""

    __tablename__ = "invoice_payments"
    __table_args__ = (
        Index("ix_payments_org_invoice", "org_id", "invoice_id"),
        Index(
            "uq_payments_org_idempotency_key",
            "org_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    invoice_id: Mapped[str] = mapped_column(
        String, ForeignKey("invoicing.invoices.id"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_method: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
