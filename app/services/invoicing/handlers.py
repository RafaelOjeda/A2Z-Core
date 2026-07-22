"""Invoicing business logic handlers called by routers.

Each handler takes an org_id and enforces org-scoping. All mutations are
audit-logged via core.audit.log_audit.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_audit, ActionType
from app.services.invoicing.db import get_session_context
from app.services.invoicing.domain import (
    InvoiceStatus,
    assert_transition_legal,
    calculate_invoice_totals,
    infer_invoice_status,
)
from app.services.invoicing.exceptions import (
    InvalidLineItemError,
    InvoiceNotFoundError,
    InvoiceStatusError,
)
from app.services.invoicing.models import (
    Invoice,
    InvoiceLineItem,
    InvoicePayment,
    InvoiceCreate,
    InvoiceUpdate,
    InvoiceRead,
    PaymentCreate,
    LineItemRead,
)


async def create_invoice(
    org_id: str,
    user_id: str,
    data: InvoiceCreate,
    invoice_counter: int,
) -> InvoiceRead:
    """Create a new draft invoice.

    Args:
        org_id: The org creating the invoice.
        user_id: The user creating it (for audit).
        data: Invoice data.
        invoice_counter: The monotonic counter from core.settings (pre-incremented).

    Returns:
        The created invoice (as InvoiceRead).

    Raises:
        InvalidLineItemError: If any line item is invalid.
    """
    # Validate line items
    for item in data.line_items:
        if item.quantity <= 0:
            raise InvalidLineItemError("Quantity must be positive")
        if item.unit_price_cents <= 0:
            raise InvalidLineItemError("Unit price must be positive")

    invoice_id = str(uuid.uuid4())
    current_year = date.today().year
    invoice_number = f"INV-{current_year}-{invoice_counter:06d}"

    # Calculate totals
    line_item_tuples = [(item.quantity, item.unit_price_cents) for item in data.line_items]
    subtotal_cents, total_cents = calculate_invoice_totals(
        line_item_tuples, data.tax_cents, data.discount_cents
    )

    async with get_session_context() as session:
        invoice = Invoice(
            org_id=org_id,
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            status=InvoiceStatus.DRAFT,
            customer_name=data.customer_name,
            customer_email=data.customer_email,
            customer_company=data.customer_company,
            invoice_date=data.invoice_date,
            due_date=data.due_date,
            payment_terms=data.payment_terms,
            subtotal_cents=subtotal_cents,
            tax_cents=data.tax_cents,
            discount_cents=data.discount_cents,
            total_cents=total_cents,
            paid_cents=0,
            notes=data.notes,
        )
        session.add(invoice)

        # Add line items
        for item in data.line_items:
            amount_cents = int(item.quantity * item.unit_price_cents)
            line_item = InvoiceLineItem(
                org_id=org_id,
                line_item_id=str(uuid.uuid4()),
                invoice_id=invoice_id,
                description=item.description,
                quantity=item.quantity,
                unit_price_cents=item.unit_price_cents,
                amount_cents=amount_cents,
            )
            session.add(line_item)

        await session.commit()

        # Audit log
        await log_audit(
            org_id=org_id,
            resource_type="invoice",
            resource_id=invoice_id,
            action_type=ActionType.CREATE,
            user_id=user_id,
            details={"invoice_number": invoice_number, "total_cents": total_cents},
        )

    return await get_invoice(org_id, invoice_id)


async def get_invoice(org_id: str, invoice_id: str) -> InvoiceRead:
    """Fetch an invoice by ID (org-scoped).

    Args:
        org_id: The org owner.
        invoice_id: The invoice ID.

    Returns:
        The invoice as InvoiceRead.

    Raises:
        InvoiceNotFoundError: If not found or wrong org.
    """
    async with get_session_context() as session:
        stmt = select(Invoice).where(
            Invoice.org_id == org_id,
            Invoice.invoice_id == invoice_id,
        )
        result = await session.execute(stmt)
        invoice = result.scalar_one_or_none()

    if not invoice:
        raise InvoiceNotFoundError(f"Invoice {invoice_id} not found")

    # Fetch line items
    async with get_session_context() as session:
        stmt = select(InvoiceLineItem).where(
            InvoiceLineItem.org_id == org_id,
            InvoiceLineItem.invoice_id == invoice_id,
        )
        result = await session.execute(stmt)
        line_items = result.scalars().all()

    return InvoiceRead(
        invoice_id=invoice.invoice_id,
        invoice_number=invoice.invoice_number,
        status=invoice.status,
        customer_name=invoice.customer_name,
        customer_email=invoice.customer_email,
        customer_company=invoice.customer_company,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        payment_terms=invoice.payment_terms,
        subtotal_cents=invoice.subtotal_cents,
        tax_cents=invoice.tax_cents,
        discount_cents=invoice.discount_cents,
        total_cents=invoice.total_cents,
        paid_cents=invoice.paid_cents,
        notes=invoice.notes,
        pdf_key=invoice.pdf_key,
        sent_at=invoice.sent_at,
        void_reason=invoice.void_reason,
        voided_at=invoice.voided_at,
        line_items=[
            LineItemRead(
                line_item_id=li.line_item_id,
                description=li.description,
                quantity=li.quantity,
                unit_price_cents=li.unit_price_cents,
                amount_cents=li.amount_cents,
            )
            for li in line_items
        ],
        created_at=invoice.created_at,
        updated_at=invoice.updated_at,
    )


async def update_invoice(
    org_id: str,
    invoice_id: str,
    user_id: str,
    data: InvoiceUpdate,
) -> InvoiceRead:
    """Update a draft invoice.

    Only draft invoices can be edited. Once sent, edits are rejected (design choice).

    Args:
        org_id: The org owner.
        invoice_id: The invoice ID.
        user_id: The user updating (for audit).
        data: Update data (all fields optional).

    Returns:
        The updated invoice.

    Raises:
        InvoiceNotFoundError: If not found.
        InvoiceStatusError: If not in draft status.
    """
    async with get_session_context() as session:
        stmt = select(Invoice).where(
            Invoice.org_id == org_id,
            Invoice.invoice_id == invoice_id,
        )
        result = await session.execute(stmt)
        invoice = result.scalar_one_or_none()

    if not invoice:
        raise InvoiceNotFoundError(f"Invoice {invoice_id} not found")

    if invoice.status != InvoiceStatus.DRAFT:
        raise InvoiceStatusError(f"Cannot edit invoice in {invoice.status} status")

    # Update fields
    if data.customer_name is not None:
        invoice.customer_name = data.customer_name
    if data.customer_email is not None:
        invoice.customer_email = data.customer_email
    if data.customer_company is not None:
        invoice.customer_company = data.customer_company
    if data.invoice_date is not None:
        invoice.invoice_date = data.invoice_date
    if data.due_date is not None:
        invoice.due_date = data.due_date
    if data.payment_terms is not None:
        invoice.payment_terms = data.payment_terms
    if data.notes is not None:
        invoice.notes = data.notes

    # If tax or discount changed, recalculate totals
    if data.tax_cents is not None or data.discount_cents is not None or data.line_items is not None:
        tax_cents = data.tax_cents if data.tax_cents is not None else invoice.tax_cents
        discount_cents = data.discount_cents if data.discount_cents is not None else invoice.discount_cents

        # If line items are provided, recalculate with them; else use existing
        if data.line_items is not None:
            line_item_tuples = [(item.quantity, item.unit_price_cents) for item in data.line_items]
            for item in data.line_items:
                if item.quantity <= 0 or item.unit_price_cents <= 0:
                    raise InvalidLineItemError("Quantity and price must be positive")
        else:
            async with get_session_context() as session:
                stmt = select(InvoiceLineItem).where(
                    InvoiceLineItem.org_id == org_id,
                    InvoiceLineItem.invoice_id == invoice_id,
                )
                result = await session.execute(stmt)
                existing_items = result.scalars().all()
                line_item_tuples = [
                    (item.quantity, item.unit_price_cents) for item in existing_items
                ]

        subtotal_cents, total_cents = calculate_invoice_totals(line_item_tuples, tax_cents, discount_cents)
        invoice.tax_cents = tax_cents
        invoice.discount_cents = discount_cents
        invoice.subtotal_cents = subtotal_cents
        invoice.total_cents = total_cents

        # Replace line items if provided
        if data.line_items is not None:
            async with get_session_context() as session:
                # Delete existing
                stmt = select(InvoiceLineItem).where(
                    InvoiceLineItem.org_id == org_id,
                    InvoiceLineItem.invoice_id == invoice_id,
                )
                result = await session.execute(stmt)
                for item in result.scalars():
                    await session.delete(item)

                # Add new ones
                for item in data.line_items:
                    amount_cents = int(item.quantity * item.unit_price_cents)
                    new_item = InvoiceLineItem(
                        org_id=org_id,
                        line_item_id=str(uuid.uuid4()),
                        invoice_id=invoice_id,
                        description=item.description,
                        quantity=item.quantity,
                        unit_price_cents=item.unit_price_cents,
                        amount_cents=amount_cents,
                    )
                    session.add(new_item)

                await session.commit()

    async with get_session_context() as session:
        session.merge(invoice)
        await session.commit()

    await log_audit(
        org_id=org_id,
        resource_type="invoice",
        resource_id=invoice_id,
        action_type=ActionType.UPDATE,
        user_id=user_id,
        details={"invoice_number": invoice.invoice_number},
    )

    return await get_invoice(org_id, invoice_id)


async def record_payment(
    org_id: str,
    invoice_id: str,
    user_id: str,
    data: PaymentCreate,
) -> InvoiceRead:
    """Record a payment against an invoice.

    Calculates the new status based on total paid. Only works on sent/partially_paid/paid invoices.

    Args:
        org_id: The org owner.
        invoice_id: The invoice ID.
        user_id: The user recording (for audit).
        data: Payment data.

    Returns:
        The updated invoice.

    Raises:
        InvoiceNotFoundError: If not found.
        InvoiceStatusError: If invoice is draft or void.
    """
    async with get_session_context() as session:
        stmt = select(Invoice).where(
            Invoice.org_id == org_id,
            Invoice.invoice_id == invoice_id,
        )
        result = await session.execute(stmt)
        invoice = result.scalar_one_or_none()

    if not invoice:
        raise InvoiceNotFoundError(f"Invoice {invoice_id} not found")

    if invoice.status == InvoiceStatus.DRAFT:
        raise InvoiceStatusError("Cannot record payment on draft invoice")
    if invoice.status == InvoiceStatus.VOID:
        raise InvoiceStatusError("Cannot record payment on void invoice")

    payment_id = str(uuid.uuid4())
    new_paid_cents = invoice.paid_cents + data.amount_cents
    new_status = infer_invoice_status(invoice.total_cents, new_paid_cents, invoice.status)

    async with get_session_context() as session:
        payment = InvoicePayment(
            org_id=org_id,
            payment_id=payment_id,
            invoice_id=invoice_id,
            amount_cents=data.amount_cents,
            payment_date=data.payment_date,
            method=data.method,
            reference=data.reference,
        )
        session.add(payment)

        # Update invoice
        stmt = select(Invoice).where(
            Invoice.org_id == org_id,
            Invoice.invoice_id == invoice_id,
        )
        result = await session.execute(stmt)
        inv = result.scalar_one()
        inv.paid_cents = new_paid_cents
        inv.status = new_status

        await session.commit()

    await log_audit(
        org_id=org_id,
        resource_type="invoice",
        resource_id=invoice_id,
        action_type=ActionType.UPDATE,
        user_id=user_id,
        details={
            "amount_cents": data.amount_cents,
            "new_status": new_status,
            "payment_date": data.payment_date,
        },
    )

    return await get_invoice(org_id, invoice_id)


async def void_invoice(
    org_id: str,
    invoice_id: str,
    user_id: str,
    reason: str,
) -> InvoiceRead:
    """Void an invoice (terminal state).

    Can void from any non-void status.

    Args:
        org_id: The org owner.
        invoice_id: The invoice ID.
        user_id: The user voiding (for audit).
        reason: Reason for voiding.

    Returns:
        The voided invoice.

    Raises:
        InvoiceNotFoundError: If not found.
        InvoiceStatusError: If already void.
    """
    async with get_session_context() as session:
        stmt = select(Invoice).where(
            Invoice.org_id == org_id,
            Invoice.invoice_id == invoice_id,
        )
        result = await session.execute(stmt)
        invoice = result.scalar_one_or_none()

    if not invoice:
        raise InvoiceNotFoundError(f"Invoice {invoice_id} not found")

    if invoice.status == InvoiceStatus.VOID:
        raise InvoiceStatusError("Invoice is already void")

    async with get_session_context() as session:
        stmt = select(Invoice).where(
            Invoice.org_id == org_id,
            Invoice.invoice_id == invoice_id,
        )
        result = await session.execute(stmt)
        inv = result.scalar_one()
        inv.status = InvoiceStatus.VOID
        inv.void_reason = reason
        from datetime import datetime
        inv.voided_at = datetime.utcnow()
        await session.commit()

    await log_audit(
        org_id=org_id,
        resource_type="invoice",
        resource_id=invoice_id,
        action_type=ActionType.UPDATE,
        user_id=user_id,
        details={"action": "void", "reason": reason},
    )

    return await get_invoice(org_id, invoice_id)


async def list_invoices(org_id: str, status_filter: str | None = None, limit: int = 50, offset: int = 0) -> list[InvoiceRead]:
    """List invoices for an org, optionally filtered by status.

    Args:
        org_id: The org owner.
        status_filter: Optional status to filter by (draft, sent, etc.).
        limit: Max results (default 50).
        offset: Offset for pagination.

    Returns:
        List of invoices.
    """
    async with get_session_context() as session:
        stmt = select(Invoice).where(Invoice.org_id == org_id)
        if status_filter:
            stmt = stmt.where(Invoice.status == status_filter)
        stmt = stmt.order_by(Invoice.created_at.desc()).limit(limit).offset(offset)

        result = await session.execute(stmt)
        invoices = result.scalars().all()

    return [await get_invoice(org_id, inv.invoice_id) for inv in invoices]
