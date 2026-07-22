"""Invoicing domain logic: state machine, status transitions, calculations.

Pure functions; all I/O is passed in or returned, never assumed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from app.services.invoicing.exceptions import InvalidStateTransitionError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class InvoiceStatus(str, Enum):
    """Invoice lifecycle states."""

    DRAFT = "draft"
    SENT = "sent"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"


def can_transition(from_status: str, to_status: str) -> bool:
    """Return True if the transition from_status → to_status is legal.

    Legal transitions:
    - draft → sent (when invoiced)
    - draft, sent, partially_paid, paid → void (terminal)
    - sent → partially_paid → paid (when payments recorded)
    - partially_paid → partially_paid (more payments, still not paid)

    Illegal (raise InvalidStateTransitionError):
    - void → anything (void is terminal)
    - draft → partially_paid, draft → paid (must send first)
    - paid → partially_paid (no refunds/adjustments in v1)
    """
    if from_status == InvoiceStatus.VOID:
        return False
    if to_status == InvoiceStatus.VOID:
        return from_status in {InvoiceStatus.DRAFT, InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID}
    if from_status == InvoiceStatus.DRAFT and to_status in {InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID}:
        return False
    if from_status == InvoiceStatus.PAID and to_status in {InvoiceStatus.PARTIALLY_PAID}:
        return False
    if from_status == to_status:
        return True
    if from_status == InvoiceStatus.DRAFT and to_status == InvoiceStatus.SENT:
        return True
    if from_status == InvoiceStatus.SENT and to_status in {InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID}:
        return True
    if from_status == InvoiceStatus.PARTIALLY_PAID and to_status in {InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID}:
        return True
    return False


def assert_transition_legal(from_status: str, to_status: str) -> None:
    """Raise InvalidStateTransitionError if the transition is illegal."""
    if not can_transition(from_status, to_status):
        raise InvalidStateTransitionError(f"Cannot transition from {from_status} to {to_status}")


def calculate_invoice_totals(
    line_items: list[tuple[Decimal, int]],
    tax_cents: int,
    discount_cents: int,
) -> tuple[int, int]:
    """Calculate subtotal and total for an invoice.

    Args:
        line_items: List of (quantity, unit_price_cents) tuples.
        tax_cents: Tax amount in cents.
        discount_cents: Discount amount in cents.

    Returns:
        (subtotal_cents, total_cents)
    """
    subtotal_cents = 0
    for quantity, unit_price_cents in line_items:
        amount = int(quantity * unit_price_cents)
        subtotal_cents += amount

    total_cents = subtotal_cents + tax_cents - discount_cents
    return subtotal_cents, max(total_cents, 0)


def infer_invoice_status(
    total_cents: int,
    paid_cents: int,
    current_status: str,
) -> str:
    """Infer the correct status based on total vs paid.

    After recording a payment, infer what the status should be.

    Args:
        total_cents: Invoice total.
        paid_cents: Amount paid so far.
        current_status: The invoice's current status (to maintain "paid" if already paid).

    Returns:
        The correct status: "paid", "partially_paid", or "sent".
    """
    if current_status == InvoiceStatus.VOID:
        return InvoiceStatus.VOID

    if paid_cents >= total_cents:
        return InvoiceStatus.PAID
    if paid_cents > 0:
        return InvoiceStatus.PARTIALLY_PAID
    return InvoiceStatus.SENT
