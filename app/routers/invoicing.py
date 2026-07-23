"""Thin HTTP layer for Invoicing (app/services/invoicing/CLAUDE.md §9).

All logic -- including access checks (§4) -- lives in
``app.services.invoicing.service``; routes just parse the request, call the
service layer, and shape the response. Errors are typed ``CoreError``
subclasses, mapped to HTTP responses by the global handler in ``app.main``
-- no per-route try/except.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.services.invoicing import service
from app.services.invoicing.db import get_session
from app.services.invoicing.models import Invoice, InvoiceLineItem
from app.services.invoicing.schemas import (
    InvoiceCreateRequest,
    InvoiceDetail,
    InvoiceListResponse,
    InvoiceUpdateRequest,
    PaymentListResponse,
    PaymentResponse,
    RecordPaymentRequest,
    RecordPaymentResponse,
    SendInvoiceRequest,
    SendInvoiceResponse,
    VoidRequest,
    VoidResponse,
)

router = APIRouter(prefix="/invoicing", tags=["invoicing"])

DbSession = Annotated[AsyncSession, Depends(get_session)]


def _detail(invoice: Invoice, line_items: list[InvoiceLineItem]) -> InvoiceDetail:
    return InvoiceDetail.from_model(
        invoice, line_items, pdf_signed_url=service.signed_pdf_url(invoice)
    )


@router.post("/orgs/{org_id}/invoices", status_code=201)
async def create_invoice(
    org_id: str, body: InvoiceCreateRequest, user: CurrentUser, session: DbSession
) -> InvoiceDetail:
    invoice, line_items = await service.create_invoice(session, org_id, user["sub"], body)
    return _detail(invoice, line_items)


@router.get("/orgs/{org_id}/invoices")
async def list_invoices(
    org_id: str,
    user: CurrentUser,
    session: DbSession,
    status: str | None = Query(default=None, description="Comma-separated status filter"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> InvoiceListResponse:
    statuses = status.split(",") if status else None
    rows, total = await service.list_invoices(
        session, org_id, user["sub"], statuses=statuses, skip=skip, limit=limit
    )
    return InvoiceListResponse(invoices=[_detail(inv, items) for inv, items in rows], total=total)


@router.get("/orgs/{org_id}/invoices/{invoice_id}")
async def get_invoice(
    org_id: str, invoice_id: str, user: CurrentUser, session: DbSession
) -> InvoiceDetail:
    invoice, line_items = await service.get_invoice(session, org_id, user["sub"], invoice_id)
    return _detail(invoice, line_items)


@router.patch("/orgs/{org_id}/invoices/{invoice_id}")
async def update_invoice(
    org_id: str, invoice_id: str, body: InvoiceUpdateRequest, user: CurrentUser, session: DbSession
) -> InvoiceDetail:
    invoice, line_items = await service.update_invoice(
        session, org_id, invoice_id, user["sub"], body
    )
    return _detail(invoice, line_items)


@router.delete("/orgs/{org_id}/invoices/{invoice_id}", status_code=204)
async def delete_invoice(
    org_id: str, invoice_id: str, user: CurrentUser, session: DbSession
) -> None:
    await service.soft_delete_invoice(session, org_id, invoice_id, user["sub"])


@router.post("/orgs/{org_id}/invoices/{invoice_id}/send")
async def send_invoice(
    org_id: str, invoice_id: str, body: SendInvoiceRequest, user: CurrentUser, session: DbSession
) -> SendInvoiceResponse:
    invoice, _ = await service.send_invoice(
        session, org_id, invoice_id, user["sub"], body.recipient_email
    )
    assert invoice.pdf_s3_key is not None  # send_invoice always sets it
    return SendInvoiceResponse(
        invoice_id=invoice.id,
        status=invoice.status,
        pdf_s3_key=invoice.pdf_s3_key,
        sent_at=invoice.updated_at,
    )


@router.post("/orgs/{org_id}/invoices/{invoice_id}/record-payment")
async def record_payment(
    org_id: str, invoice_id: str, body: RecordPaymentRequest, user: CurrentUser, session: DbSession
) -> RecordPaymentResponse:
    invoice, payment = await service.record_payment(session, org_id, invoice_id, user["sub"], body)
    return RecordPaymentResponse(
        invoice_id=invoice.id,
        status=invoice.status,
        amount_paid_cents=invoice.amount_paid_cents,
        payment_status=invoice.payment_status,
        remaining_cents=max(invoice.total_cents - invoice.amount_paid_cents, 0),
        payment_id=payment.id,
    )


@router.post("/orgs/{org_id}/invoices/{invoice_id}/void")
async def void_invoice(
    org_id: str, invoice_id: str, body: VoidRequest, user: CurrentUser, session: DbSession
) -> VoidResponse:
    invoice = await service.void_invoice(session, org_id, invoice_id, user["sub"], body.reason)
    return VoidResponse(invoice_id=invoice.id, status=invoice.status, voided_at=invoice.updated_at)


@router.get("/orgs/{org_id}/invoices/{invoice_id}/payments")
async def list_payments(
    org_id: str, invoice_id: str, user: CurrentUser, session: DbSession
) -> PaymentListResponse:
    payments = await service.list_payments(session, org_id, user["sub"], invoice_id)
    return PaymentListResponse(payments=[PaymentResponse.from_model(p) for p in payments])
