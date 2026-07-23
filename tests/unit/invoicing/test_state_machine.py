"""Unit tests for the pure invoice lifecycle functions (§3.1, §11).

No DB, no I/O -- every legal and illegal transition is asserted here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.invoicing.exceptions import InvalidStateTransitionError, InvoiceValidationError
from app.services.invoicing.models import InvoiceStatus, PaymentStatus
from app.services.invoicing.state_machine import (
    assert_can_edit,
    assert_can_record_payment,
    assert_can_send,
    assert_can_void,
    compute_line_amount_cents,
    compute_totals,
    next_invoice_status,
    next_payment_status,
)

# --- assert_can_edit ---


@pytest.mark.parametrize(
    "status",
    [
        InvoiceStatus.DRAFT.value,
        InvoiceStatus.SENT.value,
        InvoiceStatus.PARTIALLY_PAID.value,
        InvoiceStatus.PAID.value,
    ],
)
def test_can_edit_any_non_void_status(status: str) -> None:
    assert_can_edit(status)  # does not raise


def test_cannot_edit_void_invoice() -> None:
    with pytest.raises(InvalidStateTransitionError):
        assert_can_edit(InvoiceStatus.VOID.value)


# --- assert_can_send ---


def test_can_send_draft() -> None:
    assert_can_send(InvoiceStatus.DRAFT.value)  # does not raise


@pytest.mark.parametrize(
    "status",
    [
        InvoiceStatus.SENT.value,
        InvoiceStatus.PARTIALLY_PAID.value,
        InvoiceStatus.PAID.value,
        InvoiceStatus.VOID.value,
    ],
)
def test_cannot_send_non_draft(status: str) -> None:
    with pytest.raises(InvalidStateTransitionError):
        assert_can_send(status)


# --- assert_can_record_payment ---


@pytest.mark.parametrize(
    "status",
    [InvoiceStatus.SENT.value, InvoiceStatus.PARTIALLY_PAID.value, InvoiceStatus.PAID.value],
)
def test_can_record_payment_on_paid_track(status: str) -> None:
    assert_can_record_payment(status)  # does not raise


@pytest.mark.parametrize("status", [InvoiceStatus.DRAFT.value, InvoiceStatus.VOID.value])
def test_cannot_record_payment_off_paid_track(status: str) -> None:
    with pytest.raises(InvalidStateTransitionError):
        assert_can_record_payment(status)


# --- assert_can_void ---


@pytest.mark.parametrize(
    "status",
    [
        InvoiceStatus.DRAFT.value,
        InvoiceStatus.SENT.value,
        InvoiceStatus.PARTIALLY_PAID.value,
        InvoiceStatus.PAID.value,
    ],
)
def test_can_void_any_non_void_status(status: str) -> None:
    assert_can_void(status)  # does not raise


def test_cannot_void_already_void() -> None:
    with pytest.raises(InvalidStateTransitionError):
        assert_can_void(InvoiceStatus.VOID.value)


# --- next_payment_status ---


def test_payment_status_unpaid_when_nothing_paid() -> None:
    assert next_payment_status(0, 10_000) is PaymentStatus.UNPAID


def test_payment_status_partially_paid() -> None:
    assert next_payment_status(4_000, 10_000) is PaymentStatus.PARTIALLY_PAID


def test_payment_status_paid_when_fully_covered() -> None:
    assert next_payment_status(10_000, 10_000) is PaymentStatus.PAID


def test_payment_status_paid_when_overpaid() -> None:
    assert next_payment_status(11_000, 10_000) is PaymentStatus.PAID


# --- next_invoice_status ---


def test_invoice_status_never_promoted_from_draft() -> None:
    # A draft has no balance to collect -- payment_status is irrelevant to it.
    assert (
        next_invoice_status(InvoiceStatus.DRAFT.value, PaymentStatus.PAID)
        == InvoiceStatus.DRAFT.value
    )


def test_invoice_status_never_resurrected_from_void() -> None:
    assert (
        next_invoice_status(InvoiceStatus.VOID.value, PaymentStatus.PAID)
        == InvoiceStatus.VOID.value
    )


def test_invoice_status_moves_to_partially_paid() -> None:
    result = next_invoice_status(InvoiceStatus.SENT.value, PaymentStatus.PARTIALLY_PAID)
    assert result == InvoiceStatus.PARTIALLY_PAID.value


def test_invoice_status_moves_to_paid() -> None:
    result = next_invoice_status(InvoiceStatus.PARTIALLY_PAID.value, PaymentStatus.PAID)
    assert result == InvoiceStatus.PAID.value


def test_invoice_status_stays_sent_when_unpaid() -> None:
    result = next_invoice_status(InvoiceStatus.SENT.value, PaymentStatus.UNPAID)
    assert result == InvoiceStatus.SENT.value


# --- compute_line_amount_cents ---


def test_line_amount_rounds_half_up() -> None:
    # 3 * 333 cents = 999; 2.5 * 333 = 832.5 -> rounds to 833.
    assert compute_line_amount_cents(Decimal("3"), 333) == 999
    assert compute_line_amount_cents(Decimal("2.5"), 333) == 833


def test_line_amount_whole_number() -> None:
    assert compute_line_amount_cents(Decimal("3"), 15_000) == 45_000


# --- compute_totals ---


def test_compute_totals_basic() -> None:
    subtotal, total = compute_totals([45_000], tax_cents=4_050, discount_cents=0)
    assert subtotal == 45_000
    assert total == 49_050


def test_compute_totals_with_discount() -> None:
    subtotal, total = compute_totals([10_000], tax_cents=0, discount_cents=2_000)
    assert subtotal == 10_000
    assert total == 8_000


def test_compute_totals_rejects_negative_total() -> None:
    with pytest.raises(InvoiceValidationError):
        compute_totals([1_000], tax_cents=0, discount_cents=5_000)


def test_compute_totals_empty_line_items() -> None:
    subtotal, total = compute_totals([], tax_cents=500, discount_cents=0)
    assert subtotal == 0
    assert total == 500
