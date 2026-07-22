"""Unit tests for invoicing domain logic (state machine, calculations)."""

import pytest
from app.services.invoicing.domain import (
    InvoiceStatus,
    can_transition,
    assert_transition_legal,
    calculate_invoice_totals,
    infer_invoice_status,
)
from app.services.invoicing.exceptions import InvalidStateTransitionError
from decimal import Decimal


class TestStateTransitions:
    """Test the invoice state machine."""

    def test_draft_to_sent(self):
        """Draft → sent is legal."""
        assert can_transition(InvoiceStatus.DRAFT, InvoiceStatus.SENT)

    def test_sent_to_partially_paid(self):
        """Sent → partially_paid is legal."""
        assert can_transition(InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID)

    def test_sent_to_paid(self):
        """Sent → paid is legal."""
        assert can_transition(InvoiceStatus.SENT, InvoiceStatus.PAID)

    def test_partially_paid_to_paid(self):
        """Partially paid → paid is legal."""
        assert can_transition(InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID)

    def test_partially_paid_to_partially_paid(self):
        """Partially paid → partially paid is legal (idempotent)."""
        assert can_transition(InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PARTIALLY_PAID)

    def test_any_to_void(self):
        """Any non-void state → void is legal."""
        for status in [InvoiceStatus.DRAFT, InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID]:
            assert can_transition(status, InvoiceStatus.VOID), f"Failed: {status} → void"

    def test_void_is_terminal(self):
        """Void → anything is illegal."""
        for status in [InvoiceStatus.DRAFT, InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID, InvoiceStatus.VOID]:
            assert not can_transition(InvoiceStatus.VOID, status), f"Failed: void → {status}"

    def test_draft_no_payment_states(self):
        """Draft → partially_paid and draft → paid are illegal."""
        assert not can_transition(InvoiceStatus.DRAFT, InvoiceStatus.PARTIALLY_PAID)
        assert not can_transition(InvoiceStatus.DRAFT, InvoiceStatus.PAID)

    def test_paid_no_backtrack(self):
        """Paid → partially_paid is illegal (no refunds)."""
        assert not can_transition(InvoiceStatus.PAID, InvoiceStatus.PARTIALLY_PAID)

    def test_assert_transition_legal_valid(self):
        """assert_transition_legal passes for valid transitions."""
        assert_transition_legal(InvoiceStatus.DRAFT, InvoiceStatus.SENT)
        assert_transition_legal(InvoiceStatus.SENT, InvoiceStatus.PAID)

    def test_assert_transition_legal_invalid(self):
        """assert_transition_legal raises for invalid transitions."""
        with pytest.raises(InvalidStateTransitionError):
            assert_transition_legal(InvoiceStatus.DRAFT, InvoiceStatus.PAID)
        with pytest.raises(InvalidStateTransitionError):
            assert_transition_legal(InvoiceStatus.VOID, InvoiceStatus.SENT)


class TestCalculations:
    """Test invoice total calculations."""

    def test_single_line_item(self):
        """Calculate totals with a single line item."""
        # 2 units @ 10000 cents = 20000 cents subtotal
        line_items = [(Decimal("2"), 10000)]
        tax_cents = 1000
        discount_cents = 500

        subtotal, total = calculate_invoice_totals(line_items, tax_cents, discount_cents)
        assert subtotal == 20000
        assert total == 20500  # 20000 + 1000 - 500

    def test_multiple_line_items(self):
        """Calculate totals with multiple line items."""
        line_items = [
            (Decimal("1"), 5000),   # 5000
            (Decimal("2"), 3000),   # 6000
            (Decimal("10"), 200),   # 2000
        ]
        tax_cents = 1300  # 13%
        discount_cents = 0

        subtotal, total = calculate_invoice_totals(line_items, tax_cents, discount_cents)
        assert subtotal == 13000  # 5000 + 6000 + 2000
        assert total == 14300

    def test_zero_tax_and_discount(self):
        """Calculate totals with no tax or discount."""
        line_items = [(Decimal("5"), 2000)]
        subtotal, total = calculate_invoice_totals(line_items, 0, 0)
        assert subtotal == 10000
        assert total == 10000

    def test_discount_exceeds_subtotal(self):
        """Total is clamped to zero if discount > subtotal + tax."""
        line_items = [(Decimal("1"), 1000)]
        subtotal, total = calculate_invoice_totals(line_items, 0, 5000)
        assert subtotal == 1000
        assert total == 0  # Clamped to zero


class TestStatusInference:
    """Test status inference based on payment."""

    def test_no_payment_sent(self):
        """No payment leaves status as sent."""
        status = infer_invoice_status(total_cents=10000, paid_cents=0, current_status=InvoiceStatus.SENT)
        assert status == InvoiceStatus.SENT

    def test_partial_payment(self):
        """Partial payment moves to partially_paid."""
        status = infer_invoice_status(total_cents=10000, paid_cents=3000, current_status=InvoiceStatus.SENT)
        assert status == InvoiceStatus.PARTIALLY_PAID

    def test_full_payment(self):
        """Full payment moves to paid."""
        status = infer_invoice_status(total_cents=10000, paid_cents=10000, current_status=InvoiceStatus.SENT)
        assert status == InvoiceStatus.PAID

    def test_overpayment(self):
        """Overpayment is treated as paid."""
        status = infer_invoice_status(total_cents=10000, paid_cents=15000, current_status=InvoiceStatus.SENT)
        assert status == InvoiceStatus.PAID

    def test_void_stays_void(self):
        """Void status never changes."""
        status = infer_invoice_status(total_cents=10000, paid_cents=10000, current_status=InvoiceStatus.VOID)
        assert status == InvoiceStatus.VOID
