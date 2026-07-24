# Invoicing Service

> Phase 2 of the A2Z platform. **Status:** API implementation complete (Steps 1-6 of 7); pending full test coverage and PDF generation system integration.

## What is Invoicing?

Small businesses need to bill their customers and get paid. **Invoicing** lets an org create an invoice (line items, tax, discount), send it as a PDF over email, and track payment against it until it's settled or voided. It is the platform's billing service — the third proof that Core generalizes (after Core itself and Omni-Channel).

Invoicing owns its own Postgres tables and invoice state machine; everything else — who you are, which org you're in, sending the email, storing the PDF, the audit trail, the invoice number — it gets from Core.

## Documentation

- **[Design & Build Plan](../../../app/services/invoicing/CLAUDE.md)** — detailed product definition, state machine, Core dependency map, build order, scope decisions
- **[API Reference](api-reference.md)** — HTTP endpoints, request/response shapes, error codes, auth model
- **[Roadmap & Phase History](../../../docs/phase2-invoicing.md)** — short kickoff notes and scope revisions

## Core Concepts

- **Invoice** — the billable document with a formatted number (`INV-YYYY-NNNNNN`), customer, dates/terms, line items, totals in cents, and a status
- **Status** — linear progression: `draft` → `sent` → `{partially_paid, paid}` → `void` (terminal)
- **Line Item** — one billable row: description, quantity, unit price, denormalized amount
- **Payment** — one recorded receipt against an invoice: amount, date, method, reference
- **PDF** — rendered invoice uploaded to S3 on send, 1-year retention

## Quick Start

### Create an invoice

```bash
curl -X POST http://localhost:8000/v1/invoicing/orgs/my-org/invoices \
  -H "Authorization: Bearer {jwt}" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_name": "ACME Corp",
    "customer_email": "billing@acme.com",
    "invoice_date": "2026-01-15",
    "due_date": "2026-02-15",
    "line_items": [
      {"description": "Service", "quantity": 10, "unit_price_cents": 5000}
    ]
  }'
```

### Send the invoice

```bash
curl -X POST http://localhost:8000/v1/invoicing/orgs/my-org/invoices/{id}/send \
  -H "Authorization: Bearer {jwt}"
```

### Record a payment

```bash
curl -X POST http://localhost:8000/v1/invoicing/orgs/my-org/invoices/{id}/payments \
  -H "Authorization: Bearer {jwt}" \
  -H "Content-Type: application/json" \
  -d '{
    "amount_cents": 25000,
    "payment_date": "2026-01-20",
    "method": "check"
  }'
```

## Architecture

### Data Model

Three tables in a shared Postgres `invoicing` schema (same instance as Omni-Channel, different schema):

- `invoices` — org_id, invoice_id (PK), invoice_number (unique per org), status, customer info, dates, money totals, PDF key, sent/void metadata
- `invoice_line_items` — org_id, line_item_id (PK), invoice_id (FK), description, quantity, unit_price, amount
- `invoice_payments` — org_id, payment_id (PK), invoice_id (FK), amount, date, method, reference

Every table has `org_id` as the first column; every query filters on it (golden rule #2).

### State Machine

```
draft → sent → partially_paid ──┐
                     ↓          │
                  (more payments)
                     ↓          │
                    paid ←──────┘

Any status → void (terminal)
```

Illegal transitions raise `InvalidStateTransitionError`:
- Can't skip to `partially_paid`/`paid` from draft
- Can't record payment on draft or void
- Can't undo void (terminal)

### Core Dependencies

Invoicing uses Core's frozen API (no Core changes required):

| Module | Used for |
|--------|----------|
| `core.auth` | JWT validation, extract current user |
| `core.membership` | Role checks (OWNER/ADMIN for write, any member for read) |
| `core.settings` | Atomic per-org invoice counter (`get_next_invoice_number`) |
| `core.audit` | Append-only mutation log on every state change |
| `core.storage` | Upload invoice PDFs (S3, 1-year TTL, org-scoped key prefix) |
| `core.email` | Send invoice emails (suppression list + 50/hr/org rate limit already enforced) |
| `core.exceptions` | Base error class for all Invoicing errors |

## Implementation Status

### ✅ Complete (Steps 1-6)

- **Step 1 — Scaffolding:** package structure, db.py, models, Alembic setup
- **Step 2 — Schema + migrations:** baseline Alembic migration with all three tables and indexes
- **Step 3 — Domain logic:** state machine with transition validation, total calculation, status inference
- **Step 4 — CRUD handlers & routers:** create, read, update, list, void; role-based access control
- **Step 5 — PDF generation:** HTML template rendering, weasyprint integration (optional dependency)
- **Step 6 — Send invoice:** email via core.email, PDF upload to S3, status→sent workflow

### 🔄 In Progress / Pending (Step 7)

- **Full integration tests:** payment state machine edge cases, cross-org isolation on all operations (unit tests exist; integration coverage can be expanded)
- **Load testing:** latency targets from design doc
- **System integration:** weasyprint installation + testing PDF generation end-to-end
- **EventBridge events:** `invoice.paid` publishing (deferred per design scope revision 2026-07-22; Phase 3 will consume this in Omni-Channel)

## Permissions

| Action | Required Role |
|--------|---------------|
| Create / edit / send / record payment / void | OWNER or ADMIN |
| Read / list | Any member (OWNER, ADMIN, MEMBER, GUEST) |

Permissions are enforced inline in routers via `core.membership.get_membership()`.

## Errors

All errors extend `InvoicingError` (which extends `CoreError`). Each error carries a `status_code`:

- `400 InvalidLineItemError` — malformed line item
- `404 InvoiceNotFoundError` — invoice not found or wrong org
- `409 InvalidStateTransitionError` — illegal state transition (draft → paid, void → sent, etc.)
- `409 InvoiceStatusError` — can't record payment on draft/void, can't send void, can't void twice
- `500 PDFGenerationError` — PDF generation failed (e.g., weasyprint not installed)
- `429 RateLimitError` (from Core) — email rate limit exceeded
- `403 ForbiddenError` (from Core) — insufficient role

See [API Reference](api-reference.md#error-responses) for details.

## Testing

- **Unit tests** — state machine transitions (14 tests), calculations, status inference
- **Integration tests** — create/read/update/list/void/payment flows, cross-org isolation (24+ tests)
- All tests run against a real Postgres instance (via `pg_session` fixture) and moto/fakeredis for AWS/Redis

Run tests:

```bash
pytest tests/unit/invoicing/ -v
pytest tests/integration/invoicing/ -v
```

## Known Limitations (Design Scope)

The following features are **cut from v1** and deferred to Phase 3+:

- **EventBridge events** — `invoice.created/sent/paid/voided` events (Omni-Channel's commission attribution awaits `invoice.paid`)
- **AI-parse endpoint** — the `core.rate_limit` entries for AI are pre-registered but unused
- **Bulk import** — import invoices from CSV
- **Per-invoice currency** — only single currency per org (all totals in cents)
- **Line-item-level payment tracking** — payments are invoice-level only
- **Separate customer entity** — customers are inline on invoices (no dedicated table)

See the [design doc](../../../app/services/invoicing/CLAUDE.md) §15 for the full list and rationale.

## Roadmap

**Pending Phase 3 (Omni-Channel commission attribution):**
- Implement `invoice.paid` EventBridge event publishing
- Omni-Channel subscribes and attributes commission to assigned agent

**Future enhancements (v1.1+):**
- Per-org email templates for invoice body/footer
- Recurring invoices
- Multi-currency support
- Bulk operations (list export, bulk void)
- Invoice payments via link (customer-initiated payment without login)

## References

- **Design authority:** [`app/services/invoicing/CLAUDE.md`](../../../app/services/invoicing/CLAUDE.md)
- **API surface:** [`api-reference.md`](api-reference.md) (this directory)
- **Postgres schema:** [`app/services/invoicing/models.py`](../../../app/services/invoicing/models.py)
- **Alembic migrations:** [`app/services/invoicing/migrations/`](../../../app/services/invoicing/migrations/)
- **Core Design:** [`A2Z_Core_Design_TestPlan.md`](../../../A2Z_Core_Design_TestPlan.md)
- **Retention policy:** [`docs/retention.md`](../../retention.md)
