# Phase 2 — Invoicing Service: Kickoff Roadmap

> **Boundary:** Invoicing is NOT part of Core. It is a separate service living
> in `app/services/invoicing/` inside the same modular monolith. It *imports*
> Core; Core never imports it (golden rule #3, CLAUDE.md §2). The invoice
> counter already in Core's settings module is generic per-org config (Design
> §2.6), not invoicing logic. Core is frozen when Phase 2 starts — if Invoicing
> needs something Core doesn't offer, change Core deliberately and re-run the
> full Core suite (CLAUDE.md §13 Phase 2).

Build order, with the Core API each step consumes:

1. **Infra + scaffolding** — new `infra/modules/rds/` (single-AZ Postgres
   `db.t4g.micro`, private subnets, ingress from the `app` SG only) +
   `infra/live/prod/rds/`; add SQLAlchemy (async) + asyncpg + alembic deps;
   package skeleton in `app/services/invoicing/`.
2. **Schema + migrations** — `invoices`, `invoice_line_items`,
   `invoice_payments` per Design §3.2, every table keyed by `org_id`
   (org-scoping golden rule #2); Alembic baseline migration. Invoicing owns
   these tables; Core never touches them.
3. **Domain + state machine** — invoice lifecycle
   (draft → sent → partially_paid/paid → void) as pure functions; typed errors
   extending `core.exceptions.CoreError`; a unit test per transition
   (including illegal ones).
4. **CRUD routers** mounted in `app/main.py` — consumes `core.auth`
   (current user), `core.membership` (org scoping + role checks),
   `core.audit.log_audit` on every mutation, and the **settings module's
   atomic invoice counter** (`get_next_invoice_number`) for numbering.
5. **PDF generation** — render + upload via `core.storage`
   (`service_type="invoicing"`; S3 key `{org_id}/invoicing/…`, metadata +
   TTL per `docs/retention.md`).
6. **Send invoice** — `core.email.send_email` (suppression + the 50/hr/org
   `email.send` rate limit are already enforced inside Core); publish
   `invoice.sent`.
7. **AI parse endpoint** — consumes `core.rate_limit.check_and_increment`
   with the pre-registered `ai.parse` limits in `app/config.py`
   (30/min/user, 500/day/org).
8. **Events + isolation** — publish `invoice.created/sent/paid/voided` on
   `a2z-bus` via `core.events.publish_event` (document in `docs/events.md`);
   cross-org isolation integration tests mirroring Core's per-module pattern.

Exit criteria mirror Core's: ruff + mypy --strict clean, >90% coverage on the
service package, integration scenarios green, cross-org isolation proven, and
no Core test regressions.
