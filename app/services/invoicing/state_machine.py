"""Invoice lifecycle as pure functions (§3.1, §11 -- "one unit test per
transition incl. illegal ones"). No DB, no I/O: every function here takes
plain values and either returns a value or raises a typed error, so the
lifecycle can be unit-tested without Postgres.

Linear, no backtracking. ``void`` is terminal and reachable from any other
status.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.services.invoicing.exceptions import InvalidStateTransitionError, InvoiceValidationError
from app.services.invoicing.models import InvoiceStatus, PaymentStatus

_SENDABLE_STATUSES = (InvoiceStatus.DRAFT.value,)
_PAYABLE_STATUSES = (
    InvoiceStatus.SENT.value,
    InvoiceStatus.PARTIALLY_PAID.value,
    InvoiceStatus.PAID.value,
)
_PAID_TRACK_STATUSES = (
    InvoiceStatus.SENT.value,
    InvoiceStatus.PARTIALLY_PAID.value,
    InvoiceStatus.PAID.value,
)


def assert_can_edit(status: str) -> None:
    """Editing is allowed on any non-``void`` invoice (§3.1: "fully editable
    even after send" -- only the *status* never moves backwards)."""
    if status == InvoiceStatus.VOID.value:
        raise InvalidStateTransitionError("Cannot edit a voided invoice")


def assert_can_send(status: str) -> None:
    """Only a ``draft`` may be sent -- there is no ``sent -> draft`` recall in
    v1, so a second ``send`` call on an already-sent invoice is illegal."""
    if status not in _SENDABLE_STATUSES:
        raise InvalidStateTransitionError(
            f"Cannot send invoice in status {status!r}; only draft invoices can be sent"
        )


def assert_can_record_payment(status: str) -> None:
    """A payment can only land on an invoice that has been sent (draft has no
    balance to collect; void is terminal)."""
    if status not in _PAYABLE_STATUSES:
        raise InvalidStateTransitionError(
            f"Cannot record a payment on an invoice in status {status!r}"
        )


def assert_can_void(status: str) -> None:
    """``void`` is terminal -- voiding an already-void invoice is illegal, not
    a no-op, so a caller can't accidentally double-log the audit event."""
    if status == InvoiceStatus.VOID.value:
        raise InvalidStateTransitionError("Invoice is already void")


def next_payment_status(amount_paid_cents: int, total_cents: int) -> PaymentStatus:
    """Derive the denormalized payment_status from the running total."""
    if amount_paid_cents <= 0:
        return PaymentStatus.UNPAID
    if amount_paid_cents >= total_cents:
        return PaymentStatus.PAID
    return PaymentStatus.PARTIALLY_PAID


def next_invoice_status(current_status: str, payment_status: PaymentStatus) -> str:
    """Derive the invoice's lifecycle status from the payment status, but
    only once it's already on the paid track (§3.1) -- never promotes a
    ``draft`` and never resurrects a ``void`` invoice."""
    if current_status not in _PAID_TRACK_STATUSES:
        return current_status
    if payment_status is PaymentStatus.PAID:
        return InvoiceStatus.PAID.value
    if payment_status is PaymentStatus.PARTIALLY_PAID:
        return InvoiceStatus.PARTIALLY_PAID.value
    return InvoiceStatus.SENT.value


def compute_line_amount_cents(quantity: Decimal, unit_price_cents: int) -> int:
    """``round(quantity * unit_price_cents)`` to the nearest cent (§7)."""
    amount = (quantity * Decimal(unit_price_cents)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(amount)


def compute_totals(
    line_amounts_cents: list[int], tax_cents: int, discount_cents: int
) -> tuple[int, int]:
    """Return ``(subtotal_cents, total_cents)``.

    Raises:
        InvoiceValidationError: ``discount_cents`` exceeds subtotal + tax,
            which would make the invoice owe a negative amount.
    """
    subtotal_cents = sum(line_amounts_cents)
    total_cents = subtotal_cents + tax_cents - discount_cents
    if total_cents < 0:
        raise InvoiceValidationError("discount_cents exceeds subtotal + tax")
    return subtotal_cents, total_cents
