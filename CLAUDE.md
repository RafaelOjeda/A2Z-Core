# A2Z Core — Build Context for Claude Code

> **Read this first.** This file orients you (Claude Code) to build the **A2Z Core platform layer**. The authoritative API/schema spec is `A2Z_Core_Design_TestPlan.md` (in this repo). This file adds the build conventions, the gaps the design doc doesn't cover, and the order to build in. When the two conflict, this file wins for *process*; the design plan wins for *API signatures and schemas*.

> **Infra/deployment superseded, 2026-07-27 — read [`docs/architecture/single-box-mvp.md`](docs/architecture/single-box-mvp.md) for the current reality.** This file's original build plan (below) targeted a Year-12 scale (~1K orgs): ECS Fargate, ElastiCache Redis, RDS, two out-of-band Lambdas. At one actual user, that shape was collapsed to a single EC2 instance running one process: Redis is gone entirely (rate limiting, realtime fan-out, and caching all moved in-process — see `core.rate_limit`, `core.realtime`, `core.cache`); both Lambdas moved in-app (`app/dependencies.py::current_user`, `app/routers/ses_notifications.py`); the Omni-Channel worker that this plan describes as a separate ECS/EC2 process runs as a FastAPI lifespan background task in the same process instead. The **API-level content below** (module responsibilities, schemas, conventions, golden rules) is still accurate and still the spec to build against. The **infrastructure/deployment content** (stack list, module table's backing stores, §10 cost table, the ECS Fargate framing) describes a shape that no longer exists — treat it as historical intent, not current fact, and check the single-box doc plus `infra/README.md` before making any infra-related claim.

---

## 0. TL;DR for the Agent

You are building **A2Z Core**: the shared infrastructure layer that every A2Z service (Invoicing, Omni-Channel, future ones) depends on. It is **NOT a microservice** — it is a set of **Python packages** (`core/`) imported in-process by services inside a single FastAPI modular monolith, deployed to one EC2 instance (see [`docs/architecture/single-box-mvp.md`](docs/architecture/single-box-mvp.md) — this superseded the ECS Fargate target this section originally described).

Build **Core standalone and fully tested** before any service exists. Core must pass all unit + integration + load tests on its own. Invoicing and Omni-Channel come later and are just clients of Core.

**Stack:** Python 3.12, FastAPI, AWS (DynamoDB, S3, SES, SNS, EventBridge, Cognito, Secrets Manager, SQS), Terragrunt for IaC, LocalStack for local dev, pytest for tests. (No RDS, no ElastiCache Redis, no CloudWatch metrics — see the superseded-infra note above.)

**Golden rules:**
1. Every Core call is in-process (no network hop between services and Core).
2. Every data access is **org-scoped**. No query, ever, without an `org_id`.
3. Core never imports from `services/`. Services import from `core/`. Never the reverse.
4. Services talk to each other only via **EventBridge events**, never direct imports.
5. No secrets in code or env vars where avoidable — use IAM task roles.
6. Everything significant gets an **audit log** entry and **CloudWatch** trace.

---

## 1. Project Vision (Why This Exists)

A2Z is "AWS for small businesses" — a platform of focused tools (invoicing, omni-channel messaging, appointments, expenses, …) that share one backbone. One business = one **org**. Orgs subscribe to whichever services they need. Services never re-implement auth, tenancy, email, storage, or audit — they call Core.

The whole bet is: **build Core once, build it right, and every future service ships in days instead of weeks.** Your job is to make Core that dependable.

---

## 2. Repository Layout (Target State)

Create this structure. Core first; service folders are stubs until later phases.

```
a2z/
├── CLAUDE.md                      # this file
├── A2Z_Core_Design_TestPlan.md    # the authoritative spec
├── pyproject.toml                 # deps, ruff, mypy, pytest config
├── docker-compose.yml             # LocalStack + Redis for local dev
├── Dockerfile                     # single image for the monolith
├── .env.example                   # documented env vars (no real secrets)
│
├── app/
│   ├── main.py                    # FastAPI entrypoint, mounts routers, /health
│   ├── config.py                  # env loading, settings object (pydantic-settings)
│   ├── dependencies.py            # shared FastAPI deps (current_user, current_org)
│   │
│   ├── core/                      # ←★ BUILD THIS FIRST. Pure packages, no HTTP.
│   │   ├── __init__.py
│   │   ├── exceptions.py          # CoreError hierarchy (see Design §6)
│   │   ├── clients.py             # boto3 / redis client factories (singletons)
│   │   ├── auth.py                # Cognito JWT validation
│   │   ├── membership.py          # User→Org→Role (DynamoDB single-table)
│   │   ├── email.py               # SES send + suppression + status
│   │   ├── storage.py             # S3 upload/download/sign + metadata
│   │   ├── audit.py               # append-only event log (DynamoDB)
│   │   ├── settings.py            # org config + cached reads + invoice counter
│   │   ├── events.py              # ←★ NEW: EventBridge publish (see §6 below)
│   │   ├── rate_limit.py          # ←★ NEW: Redis sliding-window limiter (see §7)
│   │   ├── secrets.py             # ←★ NEW (2026-07-12): per-org/per-service credentials (Omni-Channel §6.2)
│   │   └── realtime.py            # ←★ NEW (2026-07-12): fan-out to connected clients (Omni-Channel §6.2)
│   │
│   ├── services/                  # stubs until Phase 2+
│   │   ├── invoicing/             # Phase 2
│   │   └── omnichannel/           # Phase 3
│   │
│   ├── routers/                   # thin HTTP layer over core (for admin/testing)
│   │   ├── core_admin.py          # orgs, members, settings endpoints
│   │   └── health.py
│   │
│   └── lambdas/                   # ←★ NEW: out-of-band handlers (see §5, §6)
│       ├── cognito_post_confirm.py   # create user record after signup
│       └── ses_notifications.py      # process SNS bounce/complaint → suppression
│
├── infra/                         # Terragrunt
│   ├── terragrunt.hcl
│   └── modules/                   # vpc, dynamodb, s3, ses, cognito, ecs, redis, iam
│
└── tests/
    ├── conftest.py                # LocalStack fixtures, test token factory
    ├── unit/                      # mocked deps, fast
    ├── integration/               # real-ish via LocalStack
    └── load/                      # latency targets
```

**Do not** put business logic in `routers/`. Routers are thin: parse request → call `core` → return response. All logic lives in `core/` (and later `services/`).

---

## 3. The Core Modules (Authoritative Spec Reference)

Full signatures, args, returns, errors, and performance targets are in **`A2Z_Core_Design_TestPlan.md` §2**. Do not re-derive them — implement them as written. Summary of what each owns:

| Module | Owns | Backing store | Spec |
|---|---|---|---|
| `auth` | JWT validation, claims extraction, test-token factory | Cognito JWKS (cached in-process 24h) | Design §2.1 |
| `membership` | user/org/role CRUD + queries | DynamoDB `a2z-core-membership` | Design §2.2 |
| `email` | send via SES, suppression, delivery status | SES + DynamoDB `email-events`, `suppression` | Design §2.3 |
| `storage` | S3 up/down, signed URLs, file metadata | S3 + DynamoDB `files` | Design §2.4 |
| `audit` | append-only event log + query | DynamoDB `a2z-core-audit` | Design §2.5 |
| `settings` | org config, invoice counter (no cache — pure DynamoDB reads) | DynamoDB `a2z-core-settings` | Design §2.6 |
| `events` | **NEW** publish cross-service events | EventBridge | §6 below |
| `rate_limit` | **NEW** sliding-window limits | In-process (single-box MVP — `docs/architecture/single-box-mvp.md`) | §7 below |
| `secrets` | **NEW (2026-07-12)** per-org/per-service credential access, in-process cached | Secrets Manager + `core.cache.TTLCache` | `app/services/omnichannel/CLAUDE.md` §6.2 |
| `realtime` | **NEW (2026-07-12)** fan-out to connected clients | In-process broker (single-box MVP; AppSync if ever distributed) | `app/services/omnichannel/CLAUDE.md` §6.2 |
| `cache` | **NEW** shared in-process TTL cache primitive | — | `docs/architecture/single-box-mvp.md` |

Note: `rate_limit`/`realtime`/`secrets`/`cache` were originally spec'd
against Redis (see the superseded-infra note at the top of this file). The
in-process replacement is a later, deliberate change — implement against
what's described here and in each module's docstring, not the historical
Redis wording that may still appear elsewhere in this file's older sections.

DynamoDB table schemas (PK/SK/GSI) are in **Design §3.1**. S3 key layout in **Design §3.3**. Implement exactly these — they are load-bearing for the access patterns.

---

## 4. Conventions (Follow These Everywhere)

**Language / style**
- Python 3.12, full type hints, `async def` for all I/O.
- `ruff` for lint+format, `mypy --strict` for types. Code must pass both.
- Pydantic v2 models for all DTOs (`Membership`, `Org`, `OrgSettings`, `EmailResult`, etc.).
- Docstrings on every public function: args, returns, raises, performance target.

**AWS clients**
- One place (`core/clients.py`) builds boto3 + redis clients as module-level singletons.
- Read endpoint URLs from config so LocalStack can override (`AWS_ENDPOINT_URL`).
- Never instantiate boto3 clients inside hot-path functions.

**Errors**
- Use the `CoreError` hierarchy from **Design §6** (`InvalidTokenError`, `NotFoundError`, `SuppressionListError`, `RateLimitError`, `FileTooLargeError`, …). Each carries a `status_code`.
- Routers map `CoreError.status_code` → HTTP response. Core functions raise typed errors; they never raise bare `Exception` and never return error dicts.

**Org scoping (non-negotiable)**
- Every function that touches data takes `org_id` and scopes by it.
- For DynamoDB, `org_id` is in the partition or sort key. For S3, it's the key prefix. There is no code path that reads another org's data.
- Add a unit test for every module asserting cross-org access fails.

**Audit + observability**
- Mutations call `core.audit.log_audit(...)`. Reads generally don't.
- Structured JSON logs, one line per event, with a `request_id` threaded through. Never log JWTs, full email bodies, or PII beyond what's needed.

**Idempotency**
- `create_user_if_not_exists` and SNS/webhook handlers must be idempotent (safe to call twice). Use conditional writes / unique constraints.

---

## 5. GAP — Cognito Post-Confirmation Lambda (was missing) — SUPERSEDED

> **This section's Lambda no longer exists — do not rebuild it.** On the
> single-box MVP (`docs/architecture/single-box-mvp.md`), user-row
> provisioning moved into `app/dependencies.py::current_user`, which calls
> `create_user_if_not_exists` on every authenticated request (skipped after
> the first success per user id via an in-process set). This section is
> kept for the original reasoning, which still holds — just implemented
> without a Lambda.

There must be a path that creates the Core user record the moment someone signs up. Cognito won't do it for you.

**Wire it up (historical — see the note above for what actually ships):**
- Cognito User Pool → **Post Confirmation** trigger → `app/lambdas/cognito_post_confirm.py`.
- The Lambda calls `core.membership.create_user_if_not_exists(sub, email)`.
- **Idempotent**: if the user exists, no-op. Cognito may retry the trigger.
- It must **never block signup** on a transient Core failure — log, emit a CloudWatch metric, and let signup proceed; a reconciliation job can backfill missing user rows.
- First-login org bootstrap (create a default org + owner membership) can live here OR in the first authenticated request. **Decision to make explicit in code/comments:** prefer doing it on first authenticated request so the Lambda stays minimal and signup stays fast.

**Test:** simulate the Cognito event payload shape; assert the user row is created once and twice-calling is a no-op. (Now: `tests/integration/test_dependencies.py` covers the same idempotency guarantee against `current_user` directly.)

---

## 6. GAP — EventBridge / Events Module (was missing)

Cross-service communication is **events only**. Core owns the publisher; services own their subscribers (later phases).

**`core/events.py` contract:**
```python
async def publish_event(
    org_id: str,
    event_type: str,  # dotted, e.g. "invoice.paid", "member.added", "email.bounced"
    data: dict,  # event-specific payload, JSON-serializable
    *,
    source: str = "a2z.core",  # or the service name when called from a service
) -> str:  # returns the EventBridge event id
    """
    Publish a domain event to the A2Z event bus.
    - Wraps PutEvents on a custom EventBridge bus ("a2z-bus").
    - Always includes org_id in the detail so subscribers can scope.
    - Fire-and-forget semantics from the caller's view, but await the PutEvents call
      and surface failures (do not silently drop).
    Performance: < 50ms.
    """
```

**Conventions:**
- Single custom bus: `a2z-bus`. `source` namespaces the producer (`a2z.invoicing`, `a2z.omnichannel`, `a2z.core`). `detail-type` = the `event_type`.
- Event payload schema is versioned implicitly by `event_type`; add `v2` suffixes if you must break a shape.
- Core publishes its own events too: `member.added`, `member.removed`, `member.role_changed`, `email.bounced`, `email.complained`, `settings.changed`.
- **Subscribers are not built in Phase 1.** Just build and test the publisher. Document the event catalog in a `docs/events.md` as you add producers.

**Test:** publish an event against LocalStack EventBridge; assert PutEvents was called with correct bus, source, detail-type, and that `org_id` is in the detail.

---

## 7. GAP — Rate Limiting Module (was missing)

> **Backing store superseded:** built against Redis originally (below); now
> in-process (`dict[tuple[str, str], deque[float]]`, single-box MVP — see
> `docs/architecture/single-box-mvp.md` and `docs/core/rate-limit.md`). The
> contract (function signature, error type, performance target) is
> unchanged; only "Redis sorted sets"/"Lua script" and the key format are
> historical.

Email already references "50/hour per org" but there's no limiter. Build a general one.

**`core/rate_limit.py` contract:**
```python
async def check_and_increment(
    org_id: str,
    action: str,  # e.g. "email.send", "ai.parse"
    *,
    limit: int,
    window_seconds: int,
) -> None:
    """
    Sliding-window rate limit using Redis sorted sets.
    Raises RateLimitError (status 429, with retry_after seconds) if over limit.
    Atomic via a Lua script or pipeline/MULTI to avoid race conditions.
    Key: ratelimit:{org_id}:{action}. Performance: < 10ms.
    """
```

**Initial limits to enforce (make them config-driven, not hardcoded literals scattered around):**
- `email.send`: 50 / hour / org (matches existing design note).
- `ai.parse` (future, used by Invoicing): 30 / min / user, 500 / day / org.
- Leave a registry/dict in `config.py` mapping action → (limit, window) with sane defaults so services don't invent their own.

**Test:** hammer the limiter past the threshold; assert the N+1th call raises `RateLimitError` with a `retry_after`; assert the window slides (calls succeed again after expiry).

---

## 8. GAP — SES Infrastructure & Bounce/Complaint Flow (was thin)

> **Handler superseded:** `app/lambdas/ses_notifications.py` no longer
> exists. The bounce/complaint handler described below is now
> `app/routers/ses_notifications.py` — an HTTPS route SNS calls directly
> (`aws_sns_topic_subscription` with `protocol = "https"`, no Lambda), with
> its own SNS-signature verification since it no longer has an IAM-invoke
> trust boundary. See `docs/architecture/single-box-mvp.md`. The config-set
> "exists" cache is now a process-lifetime `set`, not Redis.

The email *API* is specified; the *plumbing* must be built and documented.

**Provisioning (Terragrunt + a setup routine):**
- One **SES Configuration Set per `{org_id}-{service_type}`** (e.g., `acme-jewelry-invoicing`). Decide and implement **who creates it**: Core lazily creates the config set on first send for a new org/service pair if it doesn't exist (cache "exists" — a process-lifetime set, not Redis — to avoid repeat describe calls). Document this in `email.py`.
- Each config set has an **event destination → SNS topic** for `Bounce` and `Complaint` (and optionally `Delivery`).
- Sender identity: emails come from `{service_type}@{org.domain}` (e.g., `invoices@acme.com`), display name from `settings.sender_name`. Domain is verified once at org level.

**Bounce/complaint handler — `app/routers/ses_notifications.py` (historical text below said `app/lambdas/ses_notifications.py`):**
- Subscribed to the SNS topic(s) via an HTTPS subscription, signature-verified per request.
- Parses the SES notification, extracts recipient + bounce type / complaint.
- Writes to DynamoDB `a2z-core-suppression` scoped to the org.
- Publishes `email.bounced` / `email.complained` via `core.events.publish_event`.
- Idempotent (SNS can redeliver).

**Suppression scope decision (make it explicit):** suppression is tracked **per org**, shared across that org's services by default (a hard-bounced address is bad everywhere for that business), BUT keyed so we *can* narrow to per-service later. Implement org-level now; leave a `service_type` column nullable for future granularity. Note this in code comments so it isn't "fixed" by accident later.

**`send_email` must, in order:** resolve domain/sender from settings → ensure config set exists → check suppression → `rate_limit.check_and_increment(org_id, "email.send", ...)` → SES `SendEmail`/`SendRawEmail` (raw if attachments) → record in `email-events` → `audit.log_audit` → return `EmailResult`.

**Test:** integration test in **Design §4.2** already covers send → simulate bounce → suppression → blocked resend → unsuppress → resend. Implement against LocalStack SES + SNS.

---

## 9. GAP — DynamoDB Migration Strategy (was missing)

Core is "locked" after Phase 1, but data still evolves. Establish the rules now:

- **Additive changes (new optional attribute):** just start writing it; readers tolerate absence with defaults. No migration needed (DynamoDB is schemaless).
- **New access pattern needing a GSI:** add the GSI via Terragrunt; backfill is automatic for new items, run a one-off backfill script for old items if the pattern must cover history.
- **Renames / type changes:** dual-write (old + new) for one release, backfill, then stop writing old. Never do a destructive rename in one step.
- **Versioning items:** include a `schema_version` attribute on `METADATA` items so a future reader can branch if needed.
- Keep all backfill scripts in `infra/migrations/` with a dated filename and a docstring describing intent and rollback.

**Test:** a migration script must be re-runnable (idempotent) and have a dry-run mode.

---

## 10. GAP — Cost Model & DynamoDB Billing Mode (decide explicitly)

State the target so infra choices are intentional. Numbers are order-of-magnitude for ~1K orgs / ~3M emails per month (Year-12 target from Design §8.1) — **not** the current single-user deployment, which runs on one EC2 instance for ~$22/mo with no RDS/ElastiCache/ECS at all; see `docs/architecture/single-box-mvp.md` and `infra/README.md`'s cost table for what's actually running today.

| Component | Mode | Est. monthly |
|---|---|---|
| DynamoDB (all core tables) | **On-demand** for MVP (unpredictable, low volume) | ~$25–40 |
| SES | $0.10 / 1k emails | ~$300 at 3M (mostly customer-driven) |
| RDS Postgres (single-AZ t4g.micro) | provisioned | ~$15–25 |
| ECS Fargate (web + worker) | provisioned, autoscale | ~$20–40 |
| ElastiCache Redis (t4g.micro) | provisioned | ~$12–15 |
| S3 (logos + transient PDFs/media) | standard + lifecycle | ~$1–5 |
| EventBridge | $1 / million events | ~$1 |
| CloudWatch | free tier + overage | ~$0–5 |

**Decision:** use **DynamoDB on-demand** until access patterns are measured (avoids capacity planning while volume is small and spiky). Revisit provisioned + autoscaling only if monthly DynamoDB spend crosses ~$100. Put this threshold in `docs/cost-notes.md`.

---

## 11. GAP — Data Retention Policy (decide explicitly)

| Data | Retention | Mechanism |
|---|---|---|
| Audit log | 7 years (tax/compliance) | DynamoDB TTL attribute set on write |
| Email events | 90 days | DynamoDB TTL |
| Suppression list | Indefinite | no TTL |
| Settings | Current only (no history table in MVP) | overwrite; changes captured in audit |
| Files (S3) | Per-file TTL if set, else org lifecycle (90d active → expire) | S3 lifecycle + `files` TTL |

Set TTL attributes at write time so you never run cleanup jobs. Document in `docs/retention.md`.

---

## 12. Local Development

`docker-compose.yml` brings up **LocalStack** (DynamoDB, S3, SES, SNS, EventBridge) and **Postgres** — no Redis (single-box MVP, `docs/architecture/single-box-mvp.md`). Cognito isn't fully emulated by LocalStack's free tier — use the **test-token factory** (`auth.create_test_token`) in tests instead of real Cognito locally.

```bash
docker-compose up -d
cp .env.example .env            # points AWS_ENDPOINT_URL at LocalStack
python -m scripts.create_local_resources   # creates tables, bucket, config sets
pytest tests/unit -v            # fast, mocked
pytest tests/integration -v     # against LocalStack
pytest tests/load -m load -v    # latency checks
```

Provide `scripts/create_local_resources.py` that mirrors what Terragrunt creates in AWS (tables with correct GSIs, the S3 bucket, a sample SES config set, the EventBridge bus) so integration tests have something to hit.

---

## 13. Build Order (Do It In This Sequence)

**Phase 0 — Scaffolding (do first, in one pass)**
1. `pyproject.toml` (deps: fastapi, uvicorn, pydantic, pydantic-settings, boto3, redis, python-jose[cryptography], pytest, pytest-asyncio, moto/localstack helpers, ruff, mypy).
2. `app/config.py`, `app/core/exceptions.py`, `app/core/clients.py`.
3. `docker-compose.yml`, `.env.example`, `scripts/create_local_resources.py`.
4. `tests/conftest.py` with LocalStack + test-token fixtures.

**Phase 1 — Core, one module at a time, each fully tested before moving on**
Order chosen by dependency depth:
1. `auth.py` (no deps beyond Cognito JWKS) → unit tests.
2. `audit.py` (everything else logs to it) → unit + integration.
3. `membership.py` (depends on audit) → unit + integration + the Design §4.1 scenario.
4. `settings.py` (depends on audit; provides invoice counter) → unit + integration + §4.4.
5. `rate_limit.py` (Redis only) → unit + load.
6. `events.py` (EventBridge only) → unit + integration.
7. `storage.py` (depends on audit) → unit + integration + §4.3.
8. `email.py` (depends on settings, suppression, rate_limit, audit, events) → integration + §4.2. **Build this last — it composes the most.**
9. `app/lambdas/cognito_post_confirm.py` and `app/lambdas/ses_notifications.py`.
10. `app/main.py` + `routers/health.py` + `routers/core_admin.py` (thin endpoints to exercise Core end-to-end).
11. Run the full suite; meet the **performance targets in Design §5.4**.

**Phase 1 exit criteria (all must hold):**
- `ruff` clean, `mypy --strict` clean.
- Unit coverage > 90% on `core/`.
- All Design §4 integration scenarios pass against LocalStack.
- Load test: `get_membership` p99 < 50ms; `send_email` < 500ms; `log_audit` < 50ms (Design §5.4).
- A written test proving cross-org access is impossible for each module.

**Phase 2 — Invoicing** (separate effort; Core is frozen). Invoicing imports Core, owns its Postgres tables + state machine + PDF + AI parse, publishes `invoice.*` events. If Invoicing needs something Core doesn't offer, **change Core deliberately and re-run all Core tests** — don't add a service-specific hack into Core.

**Phase 3 — Omni-Channel** (validates Core works for two services at once).

---

## 17. Future: Website & Frontend Isolation (Phase 2+)

**Currently**, the landing page is served as a FastAPI route from `app/routers/landing.py` — part of the same process as the Core API. This is acceptable for the MVP but **must be separated in Phase 2 and beyond**.

**Rationale:**
- The marketing website (landing page, docs, status page) should not require the backend API to be operational.
- Failure of the backend should not bring down the public-facing site.
- Independent scaling, CDN caching, and incident response become possible when the website is separate.

**How to split (Phase 2+):**
1. Create a separate frontend repository (Next.js, React SPA, or static site generator).
2. Host it independently (Vercel, S3 + CloudFront, or equivalent).
3. API: `api.a2z.{domain}` or `a2z.{domain}/api/v1`.
4. Website: root domain or `www.a2z.{domain}`.
5. Remove `app/routers/landing.py` and the landing route from `app/main.py`.
6. Update `Dockerfile` and deployment to omit the website code entirely.

**Implementation note:** the landing page HTML can remain in this repo during the transition, but it should be built and deployed as a separate artifact (via a separate CI/CD pipeline) rather than baked into the API Docker image.

---

## 14. Things NOT to Build (Stay In Scope)

- **No Permissions/RBAC service.** Hardcode role checks (`role in {OWNER, ADMIN}`) in routers/services for now. Membership returns the role string; interpretation is the caller's.
- **No Billing engine.** `audit` + future usage metrics are enough; actual billing is a later service.
- **No service-to-service network auth.** It's one process; trust the in-process boundary. Only revisit if Core is ever extracted.
- **No email template system in Core.** Services render their own HTML and pass it to `email.send_email`. Core just sends.
- **No multi-region.** Single region (us-east-1) for MVP.
- **No microservice split.** Modular monolith. One image, one task family (web + worker).

---

## 15. Definition of Done (for the whole Core deliverable)

- [x] All eight `core/` modules implemented to the Design §2 signatures.
- [x] DynamoDB tables, S3 bucket, SES config-set flow, EventBridge bus provisioned via Terragrunt **and** mirrored in `create_local_resources.py`. *(Codified — data-plane + vpc/iam/cognito modules with live compositions; first real AWS apply still pending an account. The `redis`/`ecs`/`rds` modules referenced in this note as of 2026-07-12 were later deleted entirely on the single-box MVP collapse, 2026-07-27 — see `docs/architecture/single-box-mvp.md`.)*
- [x] Cognito post-confirm Lambda + SES SNS Lambda implemented and idempotent. *(Superseded 2026-07-27: both moved in-process — `app/dependencies.py::current_user` and `app/routers/ses_notifications.py` — no Lambda exists anymore. See `docs/architecture/single-box-mvp.md`.)*
- [x] `events` and `rate_limit` modules implemented (the two gaps).
- [x] Retention TTLs and on-demand billing chosen and applied.
- [x] Unit + integration + load suites green; perf targets met; cross-org isolation proven. *(81 tests, 93% core coverage — reproduced 2026-07-12 under Python 3.12.)*
- [x] `ruff` + `mypy --strict` clean.
- [x] `docs/events.md`, `docs/retention.md`, `docs/cost-notes.md` written.
- [x] `app/main.py` boots, `/health` checks DynamoDB + Postgres (was + Redis until the single-box collapse, 2026-07-27), and the admin router can create an org, add a member, change settings, and send a test email end-to-end against LocalStack. *(Exercised by `tests/integration/test_api.py::test_full_admin_flow` — moto stands in for LocalStack, same code paths.)*
- [x] `secrets` + `realtime` modules implemented to Core's bar (unit + integration tests, cross-org isolation, docstrings with perf targets) per the Omni-Channel unfreeze protocol (`app/services/omnichannel/CLAUDE.md` §6.2, Build Order Step 1). Full suite re-verified green with no regressions; Core re-frozen.

When all boxes are checked, Core is frozen and Invoicing (Phase 2) can begin.
**Status: all boxes checked — Core is frozen. Phase 2 Invoicing (design finalized 2026-07-22, v1 built 2026-07-23): `app/services/invoicing/CLAUDE.md`; short roadmap: `docs/phase2-invoicing.md`.**

**Deliberate unfreeze (Phase 3 prerequisite, 2026-07-08):** `secrets.py` and
`realtime.py` were added per `app/services/omnichannel/CLAUDE.md` §6.2 — the
two Core modules Omni-Channel needs that don't belong to any one service.
Added to the module table in §3, full suite re-run green (83 tests, 94% core
coverage, `ruff` + `mypy --strict` clean), Core re-frozen. No other module was
touched.

**Current state (updated 2026-07-20):** Phase 3 Omni-Channel is now
substantially built — not future work. It has a mounted router
(`/v1/omnichannel`, see `docs/api-reference.md` and
`docs/services/omnichannel/`), channel adapters (email/SMS/WhatsApp),
Alembic migrations, and its own test tree. Two deliberate, self-service-driven
Core additions landed since the 07-08 re-freeze and are the current Core API
surface: `core.secrets.put_secret` (write path) and
`core.email.start_domain_verification` / `get_domain_verification_status`
(per-org SES domain onboarding, exposed at
`GET /v1/core/orgs/{org_id}/domain-verification`). Whole-repo suite now
collects **286 tests** (the "81/83 tests" figures above are Core-only
historical snapshots, left as-is). The authoritative current-state reference
for anything under `docs/` is that tree, not this build log — see
`docs/README.md`.

---

## 16. Pointers

- **API signatures / schemas / test scenarios:** `A2Z_Core_Design_TestPlan.md` (§2 APIs, §3 schemas, §4 scenarios, §5 tests, §6 errors, §7 security, §8 scaling).
- **This file:** process, conventions, the five gaps (Cognito Lambda, EventBridge, rate limiting, SES plumbing, migrations), cost/retention decisions, and build order.

If something is ambiguous, prefer: org-scoped, in-process, typed, tested, and audited. That bias is almost always the A2Z-correct choice.
