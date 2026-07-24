"""Integration tests for invoicing handlers against a real Postgres."""

from datetime import date
from decimal import Decimal

import pytest

from app.services.invoicing.domain import InvoiceStatus
from app.services.invoicing.exceptions import (
    InvalidLineItemError,
    InvoiceNotFoundError,
    InvoiceStatusError,
    PDFGenerationError,
)
from app.services.invoicing.handlers import (
    create_invoice,
    get_invoice,
    list_invoices,
    record_payment,
    update_invoice,
    void_invoice,
)
from app.services.invoicing.models import (
    InvoiceCreate,
    InvoiceUpdate,
    LineItemCreate,
    PaymentCreate,
)


@pytest.mark.asyncio
async def test_create_invoice(pg_session):
    """Test creating a new invoice."""
    org_id = "test-org"
    user_id = "test-user"

    data = InvoiceCreate(
        customer_name="ACME Corp",
        customer_email="billing@acme.com",
        customer_company="ACME",
        invoice_date=date(2026, 1, 15),
        due_date=date(2026, 2, 15),
        payment_terms="Net 30",
        tax_cents=500,
        discount_cents=100,
        line_items=[
            LineItemCreate(
                description="Consulting",
                quantity=Decimal("5"),
                unit_price_cents=10000,
            ),
        ],
    )

    result = await create_invoice(org_id, user_id, data, 1)

    assert result.invoice_id is not None
    assert result.invoice_number == "INV-2026-000001"
    assert result.status == InvoiceStatus.DRAFT
    assert result.customer_name == "ACME Corp"
    assert result.subtotal_cents == 50000
    assert result.tax_cents == 500
    assert result.discount_cents == 100
    assert result.total_cents == 50400
    assert len(result.line_items) == 1


@pytest.mark.asyncio
async def test_create_invoice_invalid_line_item(pg_session):
    """Test creating an invoice with invalid line items."""
    org_id = "test-org"
    user_id = "test-user"

    data = InvoiceCreate(
        customer_name="Customer",
        customer_email="test@example.com",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 2, 1),
        line_items=[
            LineItemCreate(
                description="Bad item",
                quantity=Decimal("0"),  # Invalid
                unit_price_cents=1000,
            ),
        ],
    )

    with pytest.raises(InvalidLineItemError):
        await create_invoice(org_id, user_id, data, 1)


@pytest.mark.asyncio
async def test_get_invoice(pg_session):
    """Test fetching an invoice."""
    org_id = "test-org"
    user_id = "test-user"

    data = InvoiceCreate(
        customer_name="Test Customer",
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

    created = await create_invoice(org_id, user_id, data, 1)
    fetched = await get_invoice(org_id, created.invoice_id)

    assert fetched.invoice_id == created.invoice_id
    assert fetched.invoice_number == created.invoice_number
    assert fetched.status == InvoiceStatus.DRAFT


@pytest.mark.asyncio
async def test_get_invoice_not_found(pg_session):
    """Test fetching a non-existent invoice."""
    with pytest.raises(InvoiceNotFoundError):
        await get_invoice("test-org", "fake-id")


@pytest.mark.asyncio
async def test_get_invoice_cross_org_isolation(pg_session):
    """Test that invoices are org-scoped."""
    org1 = "org-1"
    org2 = "org-2"
    user_id = "test-user"

    data = InvoiceCreate(
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

    inv1 = await create_invoice(org1, user_id, data, 1)

    # Org2 should not see org1's invoice
    with pytest.raises(InvoiceNotFoundError):
        await get_invoice(org2, inv1.invoice_id)


@pytest.mark.asyncio
async def test_update_draft_invoice(pg_session):
    """Test updating a draft invoice."""
    org_id = "test-org"
    user_id = "test-user"

    created = await create_invoice(
        org_id,
        user_id,
        InvoiceCreate(
            customer_name="Original",
            customer_email="orig@example.com",
            invoice_date=date(2026, 1, 1),
            due_date=date(2026, 2, 1),
            line_items=[
                LineItemCreate(
                    description="Item",
                    quantity=Decimal("1"),
                    unit_price_cents=1000,
                ),
            ],
        ),
        1,
    )

    updated = await update_invoice(
        org_id,
        created.invoice_id,
        user_id,
        InvoiceUpdate(customer_name="Updated", customer_email="new@example.com"),
    )

    assert updated.customer_name == "Updated"
    assert updated.customer_email == "new@example.com"
    assert updated.status == InvoiceStatus.DRAFT


@pytest.mark.asyncio
async def test_cannot_update_sent_invoice(pg_session):
    """Test that sent invoices cannot be edited."""
    # Note: sending is not implemented in this phase, so we'd need to
    # manually set the status in the database to test this.
    # For now, this is a placeholder for the full integration test.
    pass


@pytest.mark.asyncio
async def test_record_payment(pg_session):
    """Test recording a payment against an invoice."""
    org_id = "test-org"
    user_id = "test-user"

    # Create and manually move to sent status
    created = await create_invoice(
        org_id,
        user_id,
        InvoiceCreate(
            customer_name="Customer",
            customer_email="test@example.com",
            invoice_date=date(2026, 1, 1),
            due_date=date(2026, 2, 1),
            line_items=[
                LineItemCreate(
                    description="Service",
                    quantity=Decimal("1"),
                    unit_price_cents=10000,
                ),
            ],
        ),
        1,
    )

    # For this test to work, we need to manually move to sent status
    # This will be handled in the full integration suite
    # For now, test basic payment recording logic
    payment = PaymentCreate(
        amount_cents=5000,
        payment_date=date(2026, 1, 20),
        method="check",
        reference="CHK-123",
    )

    # This will fail because the invoice is still draft
    with pytest.raises(InvoiceStatusError):
        await record_payment(org_id, created.invoice_id, user_id, payment)


@pytest.mark.asyncio
async def test_void_invoice(pg_session):
    """Test voiding an invoice."""
    org_id = "test-org"
    user_id = "test-user"

    created = await create_invoice(
        org_id,
        user_id,
        InvoiceCreate(
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
        ),
        1,
    )

    voided = await void_invoice(org_id, created.invoice_id, user_id, "Mistake")

    assert voided.status == InvoiceStatus.VOID
    assert voided.void_reason == "Mistake"
    assert voided.voided_at is not None


@pytest.mark.asyncio
async def test_cannot_void_twice(pg_session):
    """Test that voiding twice raises an error."""
    org_id = "test-org"
    user_id = "test-user"

    created = await create_invoice(
        org_id,
        user_id,
        InvoiceCreate(
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
        ),
        1,
    )

    await void_invoice(org_id, created.invoice_id, user_id, "First void")

    with pytest.raises(InvoiceStatusError):
        await void_invoice(org_id, created.invoice_id, user_id, "Second void")


@pytest.mark.asyncio
async def test_list_invoices(pg_session):
    """Test listing invoices for an org."""
    org_id = "test-org"
    user_id = "test-user"

    for i in range(3):
        await create_invoice(
            org_id,
            user_id,
            InvoiceCreate(
                customer_name=f"Customer {i}",
                customer_email=f"customer{i}@example.com",
                invoice_date=date(2026, 1, 1),
                due_date=date(2026, 2, 1),
                line_items=[
                    LineItemCreate(
                        description="Item",
                        quantity=Decimal("1"),
                        unit_price_cents=1000,
                    ),
                ],
            ),
            i + 1,
        )

    invoices = await list_invoices(org_id)
    assert len(invoices) == 3
    assert all(inv.status == InvoiceStatus.DRAFT for inv in invoices)


@pytest.mark.asyncio
async def test_list_invoices_by_status(pg_session):
    """Test filtering invoices by status."""
    org_id = "test-org"
    user_id = "test-user"

    # Create a draft invoice
    await create_invoice(
        org_id,
        user_id,
        InvoiceCreate(
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
        ),
        1,
    )

    # List only draft invoices
    drafts = await list_invoices(org_id, status_filter=InvoiceStatus.DRAFT)
    assert len(drafts) == 1
    assert drafts[0].status == InvoiceStatus.DRAFT

    # List only sent invoices (should be empty)
    sent = await list_invoices(org_id, status_filter=InvoiceStatus.SENT)
    assert len(sent) == 0


@pytest.mark.asyncio
async def test_send_invoice_pdf_generation_error(pg_session):
    """Test that send_invoice raises an error when weasyprint is not available.

    This is expected in the MVP: weasyprint has system dependencies that may
    not be available in all environments. The error is graceful.
    """
    from app.services.invoicing.handlers import send_invoice

    org_id = "test-org"
    user_id = "test-user"

    created = await create_invoice(
        org_id,
        user_id,
        InvoiceCreate(
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
        ),
        1,
    )

    # Sending should fail if weasyprint is not installed
    # (This test documents the current state; once weasyprint is installed,
    # this test should be updated to mock S3/SES)
    with pytest.raises(PDFGenerationError):
        await send_invoice(org_id, created.invoice_id, user_id, "test@example.com")
