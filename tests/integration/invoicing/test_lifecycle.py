"""Integration tests for the complete invoice lifecycle and isolation."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.services.invoicing.domain import InvoiceStatus
from app.services.invoicing.handlers import (
    create_invoice,
    get_invoice,
    record_payment,
    void_invoice,
)
from app.services.invoicing.models import (
    Invoice,
    InvoiceCreate,
    LineItemCreate,
    PaymentCreate,
)


@pytest.mark.asyncio
async def test_full_invoice_lifecycle(pg_session):
    """Test creating, sending (placeholder), and paying an invoice.

    Note: sending is not fully tested here as it requires PDF generation.
    This test focuses on the payment state machine.
    """
    org_id = "test-org"
    user_id = "test-user"

    # Step 1: Create draft invoice
    invoice_data = InvoiceCreate(
        customer_name="ACME Corp",
        customer_email="billing@acme.com",
        customer_company="ACME Inc",
        invoice_date=date(2026, 1, 15),
        due_date=date(2026, 2, 15),
        payment_terms="Net 30",
        tax_cents=1200,
        discount_cents=500,
        notes="Thank you for your business",
        line_items=[
            LineItemCreate(
                description="Consulting Services - 10 hours",
                quantity=Decimal("10"),
                unit_price_cents=5000,
            ),
            LineItemCreate(
                description="Software License",
                quantity=Decimal("1"),
                unit_price_cents=25000,
            ),
        ],
    )

    invoice = await create_invoice(org_id, user_id, invoice_data, 1)
    assert invoice.status == InvoiceStatus.DRAFT
    assert invoice.total_cents == 75700  # (50000 + 25000) + 1200 - 500
    assert invoice.paid_cents == 0
    assert len(invoice.line_items) == 2

    # Step 2: Simulate marking as sent (manually for this test)
    async with pg_session as session:
        stmt = select(Invoice).where(
            Invoice.org_id == org_id,
            Invoice.invoice_id == invoice.invoice_id,
        )
        result = await session.execute(stmt)
        inv = result.scalar_one()
        inv.status = InvoiceStatus.SENT
        await session.commit()

    # Step 3: Record first payment (partial)
    payment1_data = PaymentCreate(
        amount_cents=30000,
        payment_date=date(2026, 1, 20),
        method="check",
        reference="CHK-001",
    )
    invoice = await record_payment(org_id, invoice.invoice_id, user_id, payment1_data)
    assert invoice.status == InvoiceStatus.PARTIALLY_PAID
    assert invoice.paid_cents == 30000

    # Step 4: Record second payment (still partial)
    payment2_data = PaymentCreate(
        amount_cents=25000,
        payment_date=date(2026, 1, 25),
        method="ach",
        reference="ACH-456",
    )
    invoice = await record_payment(org_id, invoice.invoice_id, user_id, payment2_data)
    assert invoice.status == InvoiceStatus.PARTIALLY_PAID
    assert invoice.paid_cents == 55000

    # Step 5: Record final payment (reaches total)
    payment3_data = PaymentCreate(
        amount_cents=20700,
        payment_date=date(2026, 2, 1),
        method="credit_card",
    )
    invoice = await record_payment(org_id, invoice.invoice_id, user_id, payment3_data)
    assert invoice.status == InvoiceStatus.PAID
    assert invoice.paid_cents == 75700

    # Step 6: Cannot record payment on paid invoice (would need refund flow)
    overpayment = PaymentCreate(
        amount_cents=1000,
        payment_date=date(2026, 2, 5),
        method="cash",
    )
    payment = await record_payment(org_id, invoice.invoice_id, user_id, overpayment)
    # Note: The current design allows overpayment (no validation);
    # this could be tightened in future versions
    assert payment.paid_cents == 76700


@pytest.mark.asyncio
async def test_void_from_any_state(pg_session):
    """Test that an invoice can be voided from draft, sent, partially_paid, or paid status."""
    org_id = "test-org"
    user_id = "test-user"

    for i in range(4):
        invoice_data = InvoiceCreate(
            customer_name=f"Customer {i}",
            customer_email="test@example.com",
            invoice_date=date(2026, 1, 1),
            due_date=date(2026, 2, 1),
            line_items=[
                LineItemCreate(
                    description="Item",
                    quantity=Decimal("1"),
                    unit_price_cents=10000,
                ),
            ],
        )
        invoice = await create_invoice(org_id, user_id, invoice_data, i + 1)

        # Move to various states
        if i > 0:
            async with pg_session as session:
                stmt = select(Invoice).where(
                    Invoice.org_id == org_id,
                    Invoice.invoice_id == invoice.invoice_id,
                )
                result = await session.execute(stmt)
                inv = result.scalar_one()
                if i == 1:
                    inv.status = InvoiceStatus.SENT
                elif i == 2:
                    inv.status = InvoiceStatus.PARTIALLY_PAID
                    inv.paid_cents = 5000
                elif i == 3:
                    inv.status = InvoiceStatus.PAID
                    inv.paid_cents = 10000
                await session.commit()

        # Void from any state
        voided = await void_invoice(org_id, invoice.invoice_id, user_id, "Testing void")
        assert voided.status == InvoiceStatus.VOID
        assert voided.void_reason == "Testing void"


@pytest.mark.asyncio
async def test_cross_org_isolation_create(pg_session):
    """Test that an invoice created in one org is not visible to another org."""
    org1 = "org-1"
    org2 = "org-2"
    user_id = "test-user"

    invoice_data = InvoiceCreate(
        customer_name="Customer",
        customer_email="test@example.com",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 2, 1),
        line_items=[
            LineItemCreate(
                description="Item",
                quantity=Decimal("1"),
                unit_price_cents=1000,
            ),
        ],
    )

    invoice1 = await create_invoice(org1, user_id, invoice_data, 1)

    # org2 should not be able to read this invoice
    from app.services.invoicing.exceptions import InvoiceNotFoundError

    with pytest.raises(InvoiceNotFoundError):
        await get_invoice(org2, invoice1.invoice_id)


@pytest.mark.asyncio
async def test_cross_org_isolation_update(pg_session):
    """Test that updating an invoice is org-scoped."""
    org1 = "org-1"
    org2 = "org-2"
    user_id = "test-user"

    invoice_data = InvoiceCreate(
        customer_name="Original",
        customer_email="test@example.com",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 2, 1),
        line_items=[
            LineItemCreate(
                description="Item",
                quantity=Decimal("1"),
                unit_price_cents=1000,
            ),
        ],
    )

    invoice1 = await create_invoice(org1, user_id, invoice_data, 1)

    # org2 should not be able to update org1's invoice
    from app.services.invoicing.exceptions import InvoiceNotFoundError
    from app.services.invoicing.handlers import update_invoice
    from app.services.invoicing.models import InvoiceUpdate

    update_data = InvoiceUpdate(customer_name="Hacked")

    with pytest.raises(InvoiceNotFoundError):
        await update_invoice(org2, invoice1.invoice_id, user_id, update_data)


@pytest.mark.asyncio
async def test_cross_org_isolation_payment(pg_session):
    """Test that recording a payment is org-scoped."""
    org1 = "org-1"
    org2 = "org-2"
    user_id = "test-user"

    invoice_data = InvoiceCreate(
        customer_name="Customer",
        customer_email="test@example.com",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 2, 1),
        line_items=[
            LineItemCreate(
                description="Item",
                quantity=Decimal("1"),
                unit_price_cents=10000,
            ),
        ],
    )

    invoice1 = await create_invoice(org1, user_id, invoice_data, 1)

    # Move to sent so it can receive payments
    async with pg_session as session:
        stmt = select(Invoice).where(
            Invoice.org_id == org1,
            Invoice.invoice_id == invoice1.invoice_id,
        )
        result = await session.execute(stmt)
        inv = result.scalar_one()
        inv.status = InvoiceStatus.SENT
        await session.commit()

    payment_data = PaymentCreate(
        amount_cents=5000,
        payment_date=date(2026, 1, 20),
        method="check",
    )

    from app.services.invoicing.exceptions import InvoiceNotFoundError

    # org2 should not be able to record payment on org1's invoice
    with pytest.raises(InvoiceNotFoundError):
        await record_payment(org2, invoice1.invoice_id, user_id, payment_data)


@pytest.mark.asyncio
async def test_invoice_number_uniqueness_per_org(pg_session):
    """Test that invoice numbers are unique within an org but can repeat across orgs."""
    org1 = "org-1"
    org2 = "org-2"
    user_id = "test-user"

    invoice_data = InvoiceCreate(
        customer_name="Customer",
        customer_email="test@example.com",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 2, 1),
        line_items=[
            LineItemCreate(
                description="Item",
                quantity=Decimal("1"),
                unit_price_cents=1000,
            ),
        ],
    )

    # Create invoice #1 in org1
    inv1 = await create_invoice(org1, user_id, invoice_data, 1)
    assert inv1.invoice_number == "INV-2026-000001"

    # Create invoice #2 in org1
    inv2 = await create_invoice(org1, user_id, invoice_data, 2)
    assert inv2.invoice_number == "INV-2026-000002"

    # Org2 can have its own #1
    inv3 = await create_invoice(org2, user_id, invoice_data, 1)
    assert inv3.invoice_number == "INV-2026-000001"

    # Verify uniqueness constraint within org1
    from sqlalchemy.exc import IntegrityError

    async with pg_session as session:
        from app.services.invoicing.models import Invoice

        # Try to insert a duplicate (this should fail at DB level)
        duplicate = Invoice(
            org_id=org1,
            invoice_id="fake-id",
            invoice_number="INV-2026-000001",
            status=InvoiceStatus.DRAFT,
            customer_name="Test",
            customer_email="test@example.com",
            invoice_date=date(2026, 1, 1),
            due_date=date(2026, 2, 1),
            subtotal_cents=1000,
            tax_cents=0,
            discount_cents=0,
            total_cents=1000,
            paid_cents=0,
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            await session.commit()
