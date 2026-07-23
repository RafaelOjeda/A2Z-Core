"""Business logic called by the router (root CLAUDE.md §2: routers stay thin).

Plain async functions, session-first signature style, mirroring
``app/services/omnichannel``'s service-layer convention (e.g. ``routing.py``).
Each function opens with its own access check (§4) -- the same convention as
``routing.claim``'s ``access.require_role(...)`` -- so the router stays a pure
parse/call/shape layer and service-layer tests can assert permission
enforcement directly, without going through HTTP. Lifecycle validation and
pure math are delegated to ``state_machine.py``; this module owns persistence
and the calls into Core (``audit``, ``settings``, ``storage``, ``email``).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_audit
from app.core.email import EmailResult, ServiceType, send_email
from app.core.settings import get_next_invoice_number, get_org_settings
from app.core.storage import generate_signed_url, upload_file
from app.services.invoicing import access, state_machine
from app.services.invoicing.email_templates import (
    render_invoice_email_html,
    render_invoice_email_text,
)
from app.services.invoicing.exceptions import InvoiceNotFoundError
from app.services.invoicing.models import Invoice, InvoiceLineItem, InvoicePayment, InvoiceStatus
from app.services.invoicing.pdf import render_invoice_pdf
from app.services.invoicing.schemas import (
    InvoiceCreateRequest,
    InvoiceUpdateRequest,
    LineItemCreate,
    RecordPaymentRequest,
)

_PDF_SIGNED_URL_TTL = 3600  # 1 hour (§9)


async def _load_invoice(session: AsyncSession, org_id: str, invoice_id: str) -> Invoice:
    """Load an invoice scoped to ``org_id``, or raise ``InvoiceNotFoundError``.

    Same error whether the row doesn't exist, is soft-deleted, or belongs to
    another org -- cross-org existence is itself information we don't hand
    out (mirrors Omni-Channel's ``load_conversation`` convention).
    """
    stmt = select(Invoice).where(
        Invoice.org_id == org_id, Invoice.id == invoice_id, Invoice.is_deleted.is_(False)
    )
    invoice = (await session.execute(stmt)).scalar_one_or_none()
    if invoice is None:
        raise InvoiceNotFoundError(f"No invoice {invoice_id!r} in this org")
    return invoice


async def _load_line_items(
    session: AsyncSession, org_id: str, invoice_id: str
) -> list[InvoiceLineItem]:
    stmt = (
        select(InvoiceLineItem)
        .where(InvoiceLineItem.org_id == org_id, InvoiceLineItem.invoice_id == invoice_id)
        .order_by(InvoiceLineItem.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


def _build_line_items(org_id: str, items: list[LineItemCreate]) -> list[InvoiceLineItem]:
    return [
        InvoiceLineItem(
            org_id=org_id,
            description=item.description,
            quantity=item.quantity,
            unit_price_cents=item.unit_price_cents,
            amount_cents=state_machine.compute_line_amount_cents(
                item.quantity, item.unit_price_cents
            ),
        )
        for item in items
    ]


async def create_invoice(
    session: AsyncSession, org_id: str, user_id: str, body: InvoiceCreateRequest
) -> tuple[Invoice, list[InvoiceLineItem]]:
    """Create a ``draft`` invoice (§9). Assigns the number via Core's atomic
    per-org counter (§6.1) and computes totals from the line items."""
    await access.require_mutation_role(user_id, org_id)

    org_settings = await get_org_settings(org_id)
    raw_number = await get_next_invoice_number(org_id, "")
    invoice_number = f"INV-{date.today().year}-{int(raw_number):06d}"

    line_items = _build_line_items(org_id, body.line_items)
    subtotal_cents, total_cents = state_machine.compute_totals(
        [item.amount_cents for item in line_items], body.tax_cents, body.discount_cents
    )

    invoice = Invoice(
        org_id=org_id,
        invoice_number=invoice_number,
        status=InvoiceStatus.DRAFT.value,
        customer_email=body.customer_email,
        customer_name=body.customer_name,
        customer_company=body.customer_company,
        invoice_date=body.invoice_date,
        due_date=body.due_date,
        payment_terms=body.payment_terms,
        subtotal_cents=subtotal_cents,
        tax_cents=body.tax_cents,
        discount_cents=body.discount_cents,
        total_cents=total_cents,
        currency_code=org_settings.currency,
        notes=body.notes,
        created_by=user_id,
    )
    session.add(invoice)
    await session.flush()
    for item in line_items:
        item.invoice_id = invoice.id
        session.add(item)
    await session.commit()
    await session.refresh(invoice)

    await log_audit(
        org_id,
        user_id,
        "invoice.created",
        "invoice",
        invoice.id,
        {"invoice_number": invoice_number, "total_cents": total_cents},
    )
    return invoice, line_items


async def get_invoice(
    session: AsyncSession, org_id: str, user_id: str, invoice_id: str
) -> tuple[Invoice, list[InvoiceLineItem]]:
    await access.require_membership(user_id, org_id)
    invoice = await _load_invoice(session, org_id, invoice_id)
    line_items = await _load_line_items(session, org_id, invoice_id)
    return invoice, line_items


def signed_pdf_url(invoice: Invoice) -> str | None:
    if invoice.pdf_s3_key is None:
        return None
    return generate_signed_url(invoice.pdf_s3_key, expires_in=_PDF_SIGNED_URL_TTL)


async def list_invoices(
    session: AsyncSession,
    org_id: str,
    user_id: str,
    *,
    statuses: list[str] | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[tuple[Invoice, list[InvoiceLineItem]]], int]:
    """List invoices for the org (excl. soft-deleted), newest first (§9)."""
    await access.require_membership(user_id, org_id)

    base = select(Invoice).where(Invoice.org_id == org_id, Invoice.is_deleted.is_(False))
    if statuses:
        base = base.where(Invoice.status.in_(statuses))

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    stmt = base.order_by(Invoice.created_at.desc()).offset(skip).limit(limit)
    invoices = list((await session.execute(stmt)).scalars().all())

    results: list[tuple[Invoice, list[InvoiceLineItem]]] = []
    for invoice in invoices:
        items = await _load_line_items(session, org_id, invoice.id)
        results.append((invoice, items))
    return results, total


async def update_invoice(
    session: AsyncSession, org_id: str, invoice_id: str, user_id: str, body: InvoiceUpdateRequest
) -> tuple[Invoice, list[InvoiceLineItem]]:
    """Edit an invoice in place (§9). Allowed on any non-``void`` invoice;
    totals are recomputed if line items, tax, or discount change."""
    await access.require_mutation_role(user_id, org_id)

    invoice = await _load_invoice(session, org_id, invoice_id)
    state_machine.assert_can_edit(invoice.status)

    changes: dict[str, Any] = {}
    for field in (
        "customer_email",
        "customer_name",
        "customer_company",
        "invoice_date",
        "due_date",
        "payment_terms",
        "notes",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(invoice, field, value)
            changes[field] = str(value)

    recompute = False
    if body.tax_cents is not None:
        invoice.tax_cents = body.tax_cents
        changes["tax_cents"] = body.tax_cents
        recompute = True
    if body.discount_cents is not None:
        invoice.discount_cents = body.discount_cents
        changes["discount_cents"] = body.discount_cents
        recompute = True

    line_items = await _load_line_items(session, org_id, invoice_id)
    if body.line_items is not None:
        for old_item in line_items:
            await session.delete(old_item)
        await session.flush()
        line_items = _build_line_items(org_id, body.line_items)
        for item in line_items:
            item.invoice_id = invoice_id
            session.add(item)
        await session.flush()
        recompute = True
        changes["line_items"] = "replaced"

    if recompute:
        subtotal_cents, total_cents = state_machine.compute_totals(
            [item.amount_cents for item in line_items], invoice.tax_cents, invoice.discount_cents
        )
        invoice.subtotal_cents = subtotal_cents
        invoice.total_cents = total_cents
        payment_status = state_machine.next_payment_status(invoice.amount_paid_cents, total_cents)
        invoice.payment_status = payment_status.value
        invoice.status = state_machine.next_invoice_status(invoice.status, payment_status)

    await session.commit()
    await session.refresh(invoice)

    if changes:
        await log_audit(org_id, user_id, "invoice.updated", "invoice", invoice.id, changes)

    return invoice, line_items


async def soft_delete_invoice(
    session: AsyncSession, org_id: str, invoice_id: str, user_id: str
) -> None:
    """Soft-delete an invoice (§9). Allowed on any state, idempotent."""
    await access.require_mutation_role(user_id, org_id)

    invoice = await _load_invoice(session, org_id, invoice_id)
    invoice.is_deleted = True
    await session.commit()
    await log_audit(org_id, user_id, "invoice.deleted", "invoice", invoice.id, {})


async def send_invoice(
    session: AsyncSession, org_id: str, invoice_id: str, user_id: str, recipient_email: str
) -> tuple[Invoice, EmailResult]:
    """Send an invoice (§9.1): render a fresh PDF, store it, email it as an
    attachment. Only valid from ``draft``."""
    await access.require_mutation_role(user_id, org_id)

    invoice = await _load_invoice(session, org_id, invoice_id)
    state_machine.assert_can_send(invoice.status)

    line_items = await _load_line_items(session, org_id, invoice_id)
    org_settings = await get_org_settings(org_id)

    pdf_bytes = render_invoice_pdf(invoice, line_items, org_settings=org_settings)
    stored = await upload_file(
        org_id,
        "invoicing",
        f"{invoice.invoice_number}.pdf",
        pdf_bytes,
        "application/pdf",
        user_id,
    )

    email_result = await send_email(
        org_id,
        ServiceType.INVOICING,
        recipient_email,
        f"Invoice {invoice.invoice_number}",
        render_invoice_email_html(invoice, org_settings=org_settings),
        body_text=render_invoice_email_text(invoice),
        attachments=[
            {
                "content": pdf_bytes,
                "filename": f"{invoice.invoice_number}.pdf",
                "mime_type": "application/pdf",
            }
        ],
        metadata={"invoice_id": invoice.id, "invoice_number": invoice.invoice_number},
    )

    invoice.status = InvoiceStatus.SENT.value
    invoice.pdf_s3_key = stored.key
    await session.commit()
    await session.refresh(invoice)

    await log_audit(
        org_id, user_id, "invoice.sent", "invoice", invoice.id, {"recipient_email": recipient_email}
    )
    return invoice, email_result


async def record_payment(
    session: AsyncSession, org_id: str, invoice_id: str, user_id: str, body: RecordPaymentRequest
) -> tuple[Invoice, InvoicePayment]:
    """Record a manual payment (§9). Idempotent when ``idempotency_key`` is
    given -- a replay returns the existing payment instead of double-counting."""
    await access.require_mutation_role(user_id, org_id)

    invoice = await _load_invoice(session, org_id, invoice_id)
    state_machine.assert_can_record_payment(invoice.status)

    if body.idempotency_key:
        stmt = select(InvoicePayment).where(
            InvoicePayment.org_id == org_id,
            InvoicePayment.idempotency_key == body.idempotency_key,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return invoice, existing

    payment = InvoicePayment(
        org_id=org_id,
        invoice_id=invoice_id,
        amount_cents=body.amount_cents,
        payment_date=body.payment_date,
        payment_method=body.payment_method,
        reference=body.reference,
        notes=body.notes,
        idempotency_key=body.idempotency_key,
        recorded_by=user_id,
    )
    session.add(payment)

    invoice.amount_paid_cents += body.amount_cents
    payment_status = state_machine.next_payment_status(
        invoice.amount_paid_cents, invoice.total_cents
    )
    invoice.payment_status = payment_status.value
    invoice.status = state_machine.next_invoice_status(invoice.status, payment_status)

    await session.commit()
    await session.refresh(invoice)
    await session.refresh(payment)

    await log_audit(
        org_id,
        user_id,
        "invoice.payment_recorded",
        "invoice",
        invoice.id,
        {"amount_cents": body.amount_cents, "payment_method": body.payment_method},
    )
    return invoice, payment


async def void_invoice(
    session: AsyncSession, org_id: str, invoice_id: str, user_id: str, reason: str
) -> Invoice:
    """Void an invoice (§9). Terminal from any non-``void`` state."""
    await access.require_mutation_role(user_id, org_id)

    invoice = await _load_invoice(session, org_id, invoice_id)
    state_machine.assert_can_void(invoice.status)

    invoice.status = InvoiceStatus.VOID.value
    await session.commit()
    await session.refresh(invoice)

    await log_audit(org_id, user_id, "invoice.voided", "invoice", invoice.id, {"reason": reason})
    return invoice


async def list_payments(
    session: AsyncSession, org_id: str, user_id: str, invoice_id: str
) -> list[InvoicePayment]:
    await access.require_membership(user_id, org_id)
    await _load_invoice(session, org_id, invoice_id)  # ensures org-scoped existence
    stmt = (
        select(InvoicePayment)
        .where(InvoicePayment.org_id == org_id, InvoicePayment.invoice_id == invoice_id)
        .order_by(InvoicePayment.recorded_at)
    )
    return list((await session.execute(stmt)).scalars().all())
