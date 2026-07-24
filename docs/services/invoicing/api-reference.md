# Invoicing Service API Reference

> Part of the [Invoicing service docs](README.md). For the complete design, see
> [`app/services/invoicing/CLAUDE.md`](../../../app/services/invoicing/CLAUDE.md).

## Base URL

All endpoints are prefixed with `/v1/invoicing`.

## Endpoints

### Create Invoice

```http
POST /orgs/{org_id}/invoices
Content-Type: application/json
Authorization: Bearer {jwt}

{
  "customer_name": "ACME Corp",
  "customer_email": "billing@acme.com",
  "customer_company": "ACME Inc",
  "invoice_date": "2026-01-15",
  "due_date": "2026-02-15",
  "payment_terms": "Net 30",
  "tax_cents": 1200,
  "discount_cents": 500,
  "notes": "Thank you for your business",
  "line_items": [
    {
      "description": "Consulting - 10 hours",
      "quantity": 10,
      "unit_price_cents": 5000
    }
  ]
}
```

**Response:** `201 Created`

```json
{
  "invoice_id": "uuid",
  "invoice_number": "INV-2026-000001",
  "status": "draft",
  "customer_name": "ACME Corp",
  "customer_email": "billing@acme.com",
  "customer_company": "ACME Inc",
  "invoice_date": "2026-01-15",
  "due_date": "2026-02-15",
  "payment_terms": "Net 30",
  "subtotal_cents": 50000,
  "tax_cents": 1200,
  "discount_cents": 500,
  "total_cents": 50700,
  "paid_cents": 0,
  "notes": "Thank you for your business",
  "pdf_key": null,
  "sent_at": null,
  "void_reason": null,
  "voided_at": null,
  "line_items": [
    {
      "line_item_id": "uuid",
      "description": "Consulting - 10 hours",
      "quantity": 10,
      "unit_price_cents": 5000,
      "amount_cents": 50000
    }
  ],
  "created_at": "2026-01-15T10:30:00Z",
  "updated_at": "2026-01-15T10:30:00Z"
}
```

**Requires:** `OWNER` or `ADMIN` role

**Errors:**
- `400 InvalidLineItemError` — line item has negative quantity or price
- `403 ForbiddenError` — insufficient role

### Get Invoice

```http
GET /orgs/{org_id}/invoices/{invoice_id}
Authorization: Bearer {jwt}
```

**Response:** `200 OK` — returns the invoice object (same shape as Create response)

**Requires:** Any member role (OWNER, ADMIN, MEMBER, GUEST)

**Errors:**
- `404 InvoiceNotFoundError` — invoice does not exist or belongs to another org

### List Invoices

```http
GET /orgs/{org_id}/invoices?status={status}&limit=50&offset=0
Authorization: Bearer {jwt}
```

**Query Parameters:**
- `status` (optional) — filter by status: `draft`, `sent`, `partially_paid`, `paid`, `void`
- `limit` (optional, default 50) — max results per page (1-100)
- `offset` (optional, default 0) — pagination offset

**Response:** `200 OK` — returns array of invoice objects

**Requires:** Any member role

### Update Invoice

```http
PATCH /orgs/{org_id}/invoices/{invoice_id}
Content-Type: application/json
Authorization: Bearer {jwt}

{
  "customer_name": "Updated Name",
  "customer_email": "new@example.com",
  "invoice_date": "2026-01-20",
  "line_items": [
    {
      "description": "Updated item",
      "quantity": 5,
      "unit_price_cents": 10000
    }
  ]
}
```

**Response:** `200 OK` — returns updated invoice

**Requires:** `OWNER` or `ADMIN` role

**Constraints:**
- Only `draft` invoices can be edited
- Sent, partially paid, paid, and void invoices are immutable
- All fields optional; only provided fields are updated

**Errors:**
- `404 InvoiceNotFoundError` — invoice does not exist
- `409 InvoiceStatusError` — invoice is not in draft status
- `403 ForbiddenError` — insufficient role

### Send Invoice

```http
POST /orgs/{org_id}/invoices/{invoice_id}/send?recipient_email={email}
Authorization: Bearer {jwt}
```

**Query Parameters:**
- `recipient_email` (optional) — email to send to (defaults to invoice customer_email)

**Response:** `200 OK` — returns invoice with status updated to `sent`

**What happens:**
1. Generates a PDF from the invoice
2. Uploads PDF to S3 with 1-year retention (org-scoped)
3. Sends email via Core (subject to suppression list + 50/hr/org rate limit)
4. Updates invoice status to `sent` and records `sent_at` timestamp

**Requires:** `OWNER` or `ADMIN` role

**Errors:**
- `404 InvoiceNotFoundError` — invoice does not exist
- `409 InvoiceStatusError` — invoice is void (cannot send void invoices)
- `500 PDFGenerationError` — PDF generation failed (e.g., weasyprint not installed)
- `429 RateLimitError` — email rate limit exceeded (50/hour/org) — includes `Retry-After` header

### Record Payment

```http
POST /orgs/{org_id}/invoices/{invoice_id}/payments
Content-Type: application/json
Authorization: Bearer {jwt}

{
  "amount_cents": 25000,
  "payment_date": "2026-01-20",
  "method": "check",
  "reference": "CHK-001"
}
```

**Response:** `200 OK` — returns invoice with `paid_cents` updated and status adjusted

**Status transitions:**
- `sent` + payment < total → `partially_paid`
- `partially_paid` + payment < total → `partially_paid`
- `sent`/`partially_paid` + payment ≥ total → `paid`

**Requires:** `OWNER` or `ADMIN` role

**Errors:**
- `404 InvoiceNotFoundError` — invoice does not exist
- `409 InvoiceStatusError` — invoice is draft or void (cannot record payment)
- `403 ForbiddenError` — insufficient role

### Void Invoice

```http
POST /orgs/{org_id}/invoices/{invoice_id}/void?reason={reason}
Authorization: Bearer {jwt}
```

**Query Parameters:**
- `reason` (required) — reason for voiding (string, 1+ chars)

**Response:** `200 OK` — returns invoice with status `void`, `void_reason`, and `voided_at` set

**Constraints:**
- Can void from `draft`, `sent`, `partially_paid`, or `paid` status
- Void is terminal; cannot undo or transition further
- Cannot void an already-void invoice (returns 409)

**Requires:** `OWNER` or `ADMIN` role

**Errors:**
- `404 InvoiceNotFoundError` — invoice does not exist
- `409 InvoiceStatusError` — invoice is already void
- `403 ForbiddenError` — insufficient role

## Authorization

All endpoints use JWT bearer tokens and Core membership checks:

1. **Extract JWT** from `Authorization: Bearer {token}` header
2. **Validate signature** via Core's JWKS cache
3. **Check org membership** — user must be a member of the org
4. **Check role** — some endpoints require `OWNER` or `ADMIN` role

Role hierarchy (highest to lowest privilege):
- `OWNER` — full access to create, send, record payment, void, update settings
- `ADMIN` — same as owner for invoicing (can create, send, record payment, void)
- `MEMBER` — read-only access (can list and view invoices)
- `GUEST` — read-only access (can list and view invoices)

## Error Responses

All errors return JSON with `status_code`, `detail`, and `error` (exception type):

```json
{
  "detail": "Invoice not found",
  "error": "InvoiceNotFoundError"
}
```

Rate limit errors include `Retry-After` header:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 3599
Content-Type: application/json

{
  "detail": "Rate limit exceeded",
  "error": "RateLimitError"
}
```

## Org Scoping

Every endpoint is org-scoped via the `{org_id}` path parameter. An org's invoices are never visible to another org, even with the same `invoice_id`. This is enforced at the database level: every query filters by `org_id`.

## Data Retention

- **Invoice records** — kept indefinitely (business tax records)
- **PDF files** — 1-year S3 retention; older files expire automatically
- **Audit log** — 7 years (via Core's audit module)

See [`docs/retention.md`](../../retention.md) for full retention policy.
