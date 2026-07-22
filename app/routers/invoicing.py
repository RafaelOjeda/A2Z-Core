"""Invoicing HTTP routers mounted at /v1/invoicing.

Thin HTTP layer: parse request → call handlers → return response.
All auth (JWT) and membership checks happen via shared dependencies.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_from_request
from app.core.exceptions import CoreError
from app.core.membership import get_membership
from app.core.settings import get_next_invoice_number
from app.services.invoicing.handlers import (
    create_invoice,
    get_invoice,
    list_invoices,
    record_payment,
    send_invoice,
    update_invoice,
    void_invoice,
)
from app.services.invoicing.models import (
    InvoiceCreate,
    InvoiceRead,
    InvoiceUpdate,
    PaymentCreate,
)
from app.services.invoicing.db import get_session

from app.core.exceptions import ForbiddenError

router = APIRouter(prefix="/v1/invoicing", tags=["invoicing"])


async def check_access(
    org_id: str,
    request,
    required_role: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> tuple[str, str]:
    """Check JWT, org membership, and optional role. Return (user_id, org_id).

    Args:
        org_id: The org being accessed.
        request: FastAPI request.
        required_role: Optional role to require (OWNER, ADMIN, etc.).
        session: DB session.

    Returns:
        (user_id, org_id) if valid.

    Raises:
        HTTPException 401 if JWT invalid/missing.
        HTTPException 403 if not a member or role mismatch.
    """
    user = await get_current_user_from_request(request)
    membership = await get_membership(user.sub, org_id)
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this org")
    if required_role and membership.role not in {required_role, "OWNER", "ADMIN"}:
        raise HTTPException(status_code=403, detail=f"Requires {required_role} role or above")
    return user.sub, org_id


@router.post("/orgs/{org_id}/invoices", response_model=InvoiceRead)
async def create_invoice_endpoint(
    org_id: str,
    data: InvoiceCreate,
    request,
) -> InvoiceRead:
    """Create a new draft invoice.

    Requires OWNER or ADMIN role.
    """
    user_id, _ = await check_access(org_id, request, required_role="ADMIN")
    counter = await get_next_invoice_number(org_id)
    try:
        return await create_invoice(org_id, user_id, data, counter)
    except CoreError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/orgs/{org_id}/invoices/{invoice_id}", response_model=InvoiceRead)
async def get_invoice_endpoint(
    org_id: str,
    invoice_id: str,
    request,
) -> InvoiceRead:
    """Fetch an invoice by ID. Any member can read."""
    await check_access(org_id, request)
    try:
        return await get_invoice(org_id, invoice_id)
    except CoreError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.get("/orgs/{org_id}/invoices", response_model=list[InvoiceRead])
async def list_invoices_endpoint(
    org_id: str,
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    request = None,
) -> list[InvoiceRead]:
    """List invoices for an org, optionally filtered by status. Any member can read."""
    await check_access(org_id, request)
    try:
        return await list_invoices(org_id, status_filter=status, limit=limit, offset=offset)
    except CoreError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.patch("/orgs/{org_id}/invoices/{invoice_id}", response_model=InvoiceRead)
async def update_invoice_endpoint(
    org_id: str,
    invoice_id: str,
    data: InvoiceUpdate,
    request,
) -> InvoiceRead:
    """Update a draft invoice. Requires OWNER or ADMIN. Only draft invoices can be edited."""
    user_id, _ = await check_access(org_id, request, required_role="ADMIN")
    try:
        return await update_invoice(org_id, invoice_id, user_id, data)
    except CoreError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/orgs/{org_id}/invoices/{invoice_id}/payments", response_model=InvoiceRead)
async def record_payment_endpoint(
    org_id: str,
    invoice_id: str,
    data: PaymentCreate,
    request,
) -> InvoiceRead:
    """Record a payment against an invoice. Requires OWNER or ADMIN."""
    user_id, _ = await check_access(org_id, request, required_role="ADMIN")
    try:
        return await record_payment(org_id, invoice_id, user_id, data)
    except CoreError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/orgs/{org_id}/invoices/{invoice_id}/void", response_model=InvoiceRead)
async def void_invoice_endpoint(
    org_id: str,
    invoice_id: str,
    reason: str = Query(..., min_length=1),
    request = None,
) -> InvoiceRead:
    """Void an invoice. Requires OWNER or ADMIN."""
    user_id, _ = await check_access(org_id, request, required_role="ADMIN")
    try:
        return await void_invoice(org_id, invoice_id, user_id, reason)
    except CoreError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/orgs/{org_id}/invoices/{invoice_id}/send", response_model=InvoiceRead)
async def send_invoice_endpoint(
    org_id: str,
    invoice_id: str,
    recipient_email: str | None = Query(None),
    request = None,
) -> InvoiceRead:
    """Send an invoice via email (generate PDF + email). Requires OWNER or ADMIN.

    Recipient email defaults to the invoice's customer_email if not provided.
    """
    user_id, _ = await check_access(org_id, request, required_role="ADMIN")

    # Fetch invoice to get customer email if not provided
    try:
        invoice = await get_invoice(org_id, invoice_id)
        email = recipient_email or invoice.customer_email
    except CoreError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))

    try:
        return await send_invoice(org_id, invoice_id, user_id, email)
    except CoreError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
