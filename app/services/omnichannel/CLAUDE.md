# A2Z Omni-Channel — Build Context (Adapted for A2Z-Core)

> **Read this first.** This is the Omni-Channel service plan **adapted to the
> A2Z-Core repo as it actually exists**. The original service plan
> (`OmniChannel_CLAUDE.md`, external upload) and product docx remain the spec
> for *product behavior* (routing strategies, commission rules, flows). This
> file wins for *process and implementation*: which libraries, which Core
> APIs, which test harness, and what must change in Core first. Where this
> file corrects the original plan, the correction is marked **⚠ ADAPTED**.
>
> Root `CLAUDE.md` (Core conventions) still applies in full. Core is frozen:
> anything this service needs from Core is a **deliberate Core change** —
> re-run the entire Core suite (74 tests, >90% coverage bar, `ruff` +
> `mypy --strict`) before continuing (root CLAUDE.md §13, Phase 2 rule).

---

## 0. Reality Check — What Exists Today (⚠ ADAPTED)

The original plan assumed Invoicing (Phase 2) was already built. It is not:
`app/services/invoicing/` is an empty stub and only the kickoff roadmap
(`docs/phase2-invoicing.md`) exists. Consequences:

1. **Shared Postgres foundation does not exist yet.** There is no
   `infra/modules/rds/`, no SQLAlchemy/asyncpg/alembic in `pyproject.toml`.
   Whichever phase starts first (Invoicing or Omni-Channel) builds that
   foundation exactly as `docs/phase2-invoicing.md` step 1 describes; the
   other phase reuses it (shared instance, separate schema).
2. **The `invoice.paid` commission subscriber (§5.5) cannot be
   integration-tested end-to-end until Invoicing publishes that event.**
   Build the subscriber against the documented event shape (`docs/events.md`)
   with a synthetic event in tests; wire the real producer when Phase 2 lands.
3. Everything else this service needs from Core **already exists and is
   tested**: `ServiceType.OMNICHANNEL` is already in `core/email.py`, the
   `RATE_LIMITS` registry in `app/config.py` is ready for new actions, and
   `core.events.publish_event` takes `source="a2z.omnichannel"` as-is.

---

## 1. Vision (Unchanged)

A unified inbox consolidating WhatsApp, SMS, and email into one conversation
view per org. Team members claim or get assigned conversations; replies go out
through the right channel; when a conversation produces a paid invoice, the
assigned agent is credited commission.

---

## 2. Core Dependency Map — Wired to Real Signatures

### 2.1 Existing Core modules (call these; do not reimplement)

| Core API (as implemented) | Used for |
|---|---|
| `core.auth.get_current_user_from_request(request)` / `validate_jwt(token)` | JWT validation on every request. Tests use `core.auth.create_test_token(...)`. |
| `core.membership.get_membership(user_id, org_id) -> Membership \| None` | Role checks (`membership.role in {Role.OWNER, Role.ADMIN, ...}`) inline in handlers — no Permissions service. |
| `core.email.send_email(org_id, service_type=ServiceType.OMNICHANNEL, ...)` | **All outbound email** — the Email channel and internal notifications. Suppression, rate limiting (`email.send` 50/hr/org), config-set isolation, audit, and delivery events come free. Never call boto3 SES directly. |
| `core.storage.upload_file / download_file / generate_signed_url / get_file_metadata` | Inbound/outbound media. S3 keys are `{org_id}/omnichannel/...` — org prefix is enforced by `_assert_org_scope`. |
| `core.audit.log_audit(...)` | Every assignment, reassignment, and outbound send. Use existing `ActionType` values or extend the enum (a Core change — see §0). |
| `core.settings.get_org_settings(org_id) -> OrgSettings` | Timezone, verified sending domain, sender name, plan tier. Redis-cached already. |
| `core.events.publish_event(org_id, event_type, data, source="a2z.omnichannel") -> str` | Publish `message.received`, `message.sent`, `conversation.assigned`, `conversation.invoice_requested`. Update `docs/events.md` for each new type. |
| `core.rate_limit.check_and_increment(org_id, action, limit=..., window_seconds=...)` | Per-channel outbound limits. Register actions in `app/config.py::RATE_LIMITS` (see §2.4) and read them via `core.rate_limit.limits_for(action)` — never hardcode literals. |
| `core.exceptions.CoreError` | Base of this service's error hierarchy (§6). Each error carries `status_code`; routers map it to HTTP. |
| `core.logging.get_logger(name)` | Structured JSON logs. Do not configure logging yourself. |
| `core.clients` | The **only** place AWS/Redis clients are built. New clients this service needs (Secrets Manager, AppSync) are added here as `@lru_cache` factories, and every sync boto3 call goes through `await core.clients.run_aws(fn, ...)`. |

### 2.2 NEW Core modules required (promote, don't duplicate)

Same as the original plan — `core/secrets.py` and `core/realtime.py` are Core
modules, not Omni-Channel-private code. **Unfreeze protocol:** add them to the
root `CLAUDE.md` module table, meet the same bar (unit + integration tests,
>90% coverage, cross-org isolation test, docstrings with perf targets), re-run
the full Core suite green, then re-freeze Core before writing any
Omni-Channel-specific code.

**`app/core/secrets.py`** — per-org, per-service credential access.

```python
async def get_secret(org_id: str, service_type: str, key: str) -> dict[str, Any]:
    """Fetch a secret for an org/service pair (e.g. WhatsApp token).

    - Backed by AWS Secrets Manager; name convention: a2z/{org_id}/{service_type}/{key}.
    - Client: new @lru_cache factory in core/clients.py; calls via run_aws().
    - Cached in Redis (shared client, key ``secret:{org_id}:{service_type}:{key}``)
      with a 5-minute TTL — same idiom core.settings already uses.
    - Never logs secret values. Logs only org_id, service_type, key, hit/miss.

    Raises: SecretNotFoundError (CoreError, 404).
    Performance: < 20ms cache hit, < 200ms cache miss.
    """
```

> **⚠ ADAPTED:** the original plan mandated the AWS SDK's *Secrets Manager
> Caching Client* (`aws-secretsmanager-caching`). That library is sync-only,
> adds a dependency, and duplicates a caching idiom Core already has (Redis
> TTL cache in `core.settings`, JWKS cache in `core.auth`). Use the Redis
> pattern instead. Cache invalidation on rotation: delete the Redis key from
> the rotation path, or accept the ≤5-min staleness window (document choice).

**`app/core/realtime.py`** — real-time fan-out to connected clients.

```python
async def publish_update(org_id: str, channel: str, payload: dict[str, Any]) -> None:
    """Push a real-time update to connected clients via AppSync.

    - channel examples: f"org:{org_id}:conversations",
      f"conversation:{conversation_id}:messages"
    - Wraps an AppSync GraphQL mutation that fans out to subscribers. The
      HTTP call to the AppSync endpoint uses httpx (see §3 deps) with
      IAM SigV4 signing via botocore's SigV4Auth — no new signing library.
    - Fire-and-forget from caller's perspective; errors logged, not swallowed.

    Raises: RealtimeError (CoreError, 502) only on config errors, not fan-out failures.
    Performance: < 100ms.
    """
```

> **Test-harness note (⚠ ADAPTED):** Core's integration tests run on **moto +
> fakeredis**, not LocalStack. moto supports Secrets Manager (add the
> `secretsmanager` extra) but has no usable AppSync data plane — unit-test
> `core.realtime` against a stubbed HTTP transport (httpx `MockTransport`)
> and flag real-AWS verification explicitly in the test plan, don't skip it
> silently.

### 2.3 Cross-service rule (unchanged, non-negotiable)

Omni-Channel and Invoicing never import each other's code. Communication is
events only:
- Omni-Channel publishes `conversation.invoice_requested` → Invoicing consumes.
- Invoicing publishes `invoice.paid` → Omni-Channel consumes, credits commission.

**⚠ ADAPTED:** Core owns only the *publisher* (`core.events`). Subscribers are
service-owned: an EventBridge **rule** on `a2z-bus` (`source = a2z.invoicing`,
`detail-type = invoice.paid`) targets this service's **events SQS queue**; the
Fargate worker consumes it. That rule + queue live in this service's
Terragrunt modules (§11).

### 2.4 Rate-limit registry additions (`app/config.py`)

```python
RATE_LIMITS: dict[str, tuple[int, int]] = {
    # ... existing entries ...
    "omnichannel.whatsapp.send": (80, 1),      # Meta pair-rate ceiling; tune per tier
    "omnichannel.sms.send": (60, 60),          # provider throughput cap per org
    "omnichannel.ai.classify": (500, 86400),   # Bedrock cost guard per org
    # email channel needs no new entry: core.email already enforces email.send
}
```

Exact numbers are provider-dependent — set them from the provider contracts at
build time; the point is they live here, config-driven, like everything else.

---

## 3. Stack & Libraries (⚠ ADAPTED — locked to A2Z-Core's pyproject)

Python 3.12 + FastAPI, same repo, same monolith. The original plan's
TypeScript adapter contract was already translated to Python; this section
locks the *libraries*:

**Already in `pyproject.toml` — use these, add nothing that duplicates them:**

| Need | Use (already pinned) |
|---|---|
| AWS calls | `boto3` (sync) via `core.clients` factories + `await run_aws(...)`. **No aioboto3.** |
| Redis (presence, caches, rate limits) | `redis.asyncio` via `core.clients.redis_client()` — shared client, namespaced keys (`presence:{org_id}:*`, `secret:*`, `aiclass:{content_hash}`, `mediaurl:{key}`). |
| DTOs / validation | `pydantic` v2 — all adapter types (`NormalizedInboundMessage`, `OutboundContent`, `SendResult`, `DeliveryStatusUpdate`, `SupportedFeatures`), models in `models.py`. |
| Config | `pydantic-settings` — new fields go on `app/config.py::Settings` with env aliases (e.g. `APPSYNC_ENDPOINT`, `DATABASE_URL`, SQS queue URLs). No per-service config module. |
| JWT | `python-jose` — already wrapped by `core.auth`; never used directly here. |
| Webhook signatures | stdlib `hmac` + `hashlib` (Meta `X-Hub-Signature-256`, Twilio-style HMAC). **No new dependency.** |
| Lint / types | `ruff` (line-length 100, rules E,F,I,B,UP,ASYNC) and `mypy --strict` with the pydantic plugin — config already in `pyproject.toml`; do not add per-service overrides. |
| Tests | `pytest` + `pytest-asyncio` (asyncio_mode=auto), `moto` for AWS, `fakeredis` for Redis, `httpx` for API tests — same harness as Core. **Not LocalStack** (docker-compose LocalStack exists for manual dev only; CI/tests use moto). |

**New dependencies this service is allowed to add:**

| Dependency | Why | Where |
|---|---|---|
| `sqlalchemy[asyncio]`, `asyncpg`, `alembic` | Postgres data layer — same deps `docs/phase2-invoicing.md` step 1 already earmarks for Invoicing. Add once; both services share them. | `[project.dependencies]` |
| `httpx` | Runtime HTTP client for WhatsApp Graph API, SMS provider, AppSync mutations. Already a dev dep — promote to runtime. | move to `[project.dependencies]` |
| `moto[secretsmanager]`, `boto3-stubs[secretsmanager,sqs]` extras | Test/typing coverage for the new AWS surfaces. | `[project.optional-dependencies].dev` |

**Explicitly NOT added (⚠ ADAPTED deviations from the original plan):**

- `aws-secretsmanager-caching` — replaced by the Redis cache idiom (§2.2).
- **Sentry** — the original plan kept Sentry alongside CloudWatch. That
  violates the AWS-only principle and adds a dependency Core doesn't have.
  CloudWatch Logs Insights + metric alarms + X-Ray cover it. If exception
  grouping proves genuinely missing, revisit deliberately — don't start there.
- Any WebSocket library — real-time is AppSync via `core.realtime`, period.
- A second Bedrock/AI client config — reuse the one Invoicing's AI parse will
  define; if this service ships first, define it in `core/clients.py` so
  Invoicing inherits it.

**Adapter contract (Python, final):**

```python
# app/services/omnichannel/adapters/base.py
from typing import Protocol, runtime_checkable

@runtime_checkable
class ChannelAdapter(Protocol):
    """Every channel implements this. One file per channel. Nothing else in
    the system may know which channel it's talking to beyond this contract."""

    supported_features: "SupportedFeatures"

    async def verify_inbound_signature(self, raw_body: bytes, headers: dict[str, str], secret: str) -> bool: ...
    async def normalize_inbound(self, raw_payload: dict[str, Any]) -> list["NormalizedInboundMessage"]: ...
    async def send_outbound(self, to: str, content: "OutboundContent", credentials: dict[str, Any]) -> "SendResult": ...
    async def interpret_delivery_webhook(self, raw_payload: dict[str, Any]) -> list["DeliveryStatusUpdate"]: ...
```

Shared types are Pydantic models in `adapters/types.py`. Adding a channel =
one new file + one registry entry. Nothing else changes.

---

## 4. Repo Layout (extends what's already here)

```
A2Z-Core/
├── app/
│   ├── core/                          # frozen; unfrozen once for:
│   │   ├── secrets.py                 # ←★ NEW (§2.2)
│   │   └── realtime.py                # ←★ NEW (§2.2)
│   ├── services/omnichannel/          # ←★ THIS SERVICE (stub exists)
│   │   ├── models.py                  # Pydantic DTOs + SQLAlchemy tables
│   │   ├── db.py                      # async engine/session, "omnichannel" schema
│   │   ├── adapters/                  # base.py, types.py, whatsapp.py, sms.py, email.py, registry.py
│   │   ├── routing.py                 # round-robin / sticky / single-assignee
│   │   ├── presence.py                # Redis-backed presence
│   │   ├── commission.py              # attribution + invoice.paid handler
│   │   ├── handlers.py                # business logic called by routers
│   │   ├── webhooks/                  # whatsapp_webhook.py, sms_webhook.py, ses_inbound.py
│   │   └── worker.py                  # Fargate worker: SQS consumer
│   ├── routers/omnichannel.py         # thin HTTP layer, mounted in app/main.py
│   └── lambdas/                       # thin entrypoints → services/omnichannel/webhooks
│       ├── omnichannel_whatsapp_webhook.py
│       ├── omnichannel_sms_webhook.py
│       └── omnichannel_ses_inbound.py
├── infra/
│   ├── modules/                       # NEW: rds (if not built by Phase 2 yet), appsync,
│   │   ...                            #      secretsmanager-channels, sqs-omnichannel,
│   │                                  #      ses-receipt-rules, omnichannel-worker, lambda-webhooks
│   └── live/prod/<name>/terragrunt.hcl   # one live folder per module — follow existing layout
└── tests/{unit,integration,load}/omnichannel/
```

**Reused, not re-provisioned:** the RDS instance (new `omnichannel` schema),
the Redis cluster (new key namespaces), the SES sending pipeline (via
`core.email`), Core's VPC/IAM Terragrunt modules. Alembic migrations for this
schema live with the service; DynamoDB-style backfill scripts stay in
`infra/migrations/` per root CLAUDE.md §9.

---

## 5. Service-Specific Design (product spec unchanged; deltas only)

- **Data model** (docx §6): Postgres in the shared RDS instance, `omnichannel`
  schema. The `(channel_type, external_message_id)` unique constraint on
  `messages` is the webhook-idempotency guarantee — non-negotiable. All
  indexes at table-creation time. Every table carries `org_id` and every query
  filters on it (golden rule #2) — plus a cross-org-access-fails test per table.
- **Email adapter** (§5.2 of original): `send_outbound` calls
  `core.email.send_email(org_id, service_type=ServiceType.OMNICHANNEL, ...)`.
  The enum value already exists. Inbound email (SES receipt rule → S3 →
  Lambda) is service-owned; the Lambda reads raw MIME via `core.storage` and
  feeds `normalize_inbound` like any other channel. MIME parsing: stdlib
  `email` package — no new dependency.
- **Routing & presence**: live state in Redis (`presence:{org_id}:*` via
  `core.clients.redis_client()`); Postgres `presence` row is backup/audit only.
- **Real-time inbox**: AppSync via `core.realtime.publish_update`; re-check
  `core.membership.get_membership` on subscribe; idle-tab long-poll fallback
  after 5 min.
- **Commission**: snapshot `assigned_user_id` at **invoice-creation** time
  (load-bearing rule — not payment time). `conversation_assignments` is
  append-only; attributions are replayable; refunds set `status='reversed'`,
  never delete.
- **Message flows**: implement docx §7–§8 exactly (verify → dedupe → enqueue →
  process → fan-out → route → surface, mirrored outbound). Webhook Lambdas ack
  fast and enqueue to SQS; the worker does the heavy lifting.

---

## 6. Errors (wired to Core's hierarchy)

```python
# app/services/omnichannel/exceptions.py
from app.core.exceptions import CoreError

class OmniChannelError(CoreError): ...                      # base, 500
class ChannelAdapterError(OmniChannelError): ...            # 502
class WebhookSignatureError(OmniChannelError): ...          # 401
class RoutingError(OmniChannelError): ...                   # 500
class CommissionError(OmniChannelError): ...                # 409
class ConversationNotFoundError(OmniChannelError): ...      # 404
```

Each sets `status_code` exactly as Core's errors do; the existing router
error-mapping handles them with zero new plumbing. `RateLimitError`,
`SuppressionListError`, `StorageNotFoundError` etc. are raised *by Core* and
pass through untouched.

---

## 7. AWS Service Map (unchanged from original plan)

Fargate API (same task family pattern) · Lambda + API Gateway webhooks ·
Fargate SQS worker · shared RDS Postgres · shared ElastiCache Redis · S3 via
`core.storage` · SQS (per-channel inbound + outbound + events, DLQs on all) ·
EventBridge via `core.events` · AppSync via `core.realtime` · Secrets Manager
via `core.secrets` · Postgres full-text until p95 > 200ms · Bedrock (Haiku
classify / Sonnet summarize). The one non-AWS dependency is Meta's Graph API —
the channel endpoint itself.

---

## 8. Cost & Caching (all via the shared Redis client)

Same table as the original plan; every cache uses `core.clients.redis_client()`
with a namespaced key — no second cache system:

| Cost vector | Mitigation (Redis key / mechanism) |
|---|---|
| Bedrock calls | `aiclass:{content_hash}`, 24h TTL; suggested replies on explicit click only |
| SES volume | batch notifications; org's own verified domain (Core config sets already per org/service) |
| SMS | 10DLC upfront; sticky-route; backoff, never blind retry |
| AppSync minutes | idle tabs → long-poll after 5 min |
| RDS IO | >12-month messages to cold partition |
| S3 egress | `mediaurl:{key}` signed-URL cache, 1h TTL (< signed expiry) |
| NAT Gateway | VPC endpoints for S3, SQS, Secrets Manager, SES, Bedrock — extend the existing `vpc` module |
| Secrets Manager API | 5-min Redis TTL in `core.secrets` (§2.2) |

---

## 9. Conventions (identical to Core — no exceptions)

Full type hints · `async def` for all I/O (sync boto3 always through
`run_aws`) · `ruff` + `mypy --strict` clean · Pydantic v2 DTOs · docstrings
with args/returns/raises/perf target · every mutation → `core.audit.log_audit`
· every cross-service fact → `core.events.publish_event` · org-scoping on
every query + a cross-org test per table · idempotent webhook/SQS handlers
(the unique constraint) · structured logs via `core.logging.get_logger` only.

---

## 10. Observability (CloudWatch only — ⚠ ADAPTED: no Sentry)

- Structured JSON logs via `core.logging` with `request_id` /
  `conversation_id` / `message_id` threaded through.
- X-Ray on all Lambdas + the worker/API; webhook → SQS → worker → EventBridge
  → AppSync traceable as one trace.
- Metrics namespace `A2Z/OmniChannel`: `WebhookAckLatencyMs` (p99 alarm > 2s),
  `MessageProcessingLatencyMs`, `RoutingLatencyMs`, `SendSuccessRate`/
  `SendFailureRate` per channel, `AICacheHitRate`, `ActiveAppSyncConnections`.
- Alarms: any DLQ depth > 0, webhook ack p99 breach, send failure rate > 5%.

---

## 11. Terragrunt (follow the existing `infra/` layout exactly)

New `infra/modules/`: `rds/` (only if Phase 2 hasn't built it), `appsync/`,
`secretsmanager-channels/`, `sqs-omnichannel/` (includes the EventBridge rule
targeting the events queue, §2.3), `ses-receipt-rules/`,
`omnichannel-worker/`, `lambda-webhooks/`. Each gets a matching
`infra/live/prod/<name>/terragrunt.hcl` like the nine existing ones. Reused
unmodified: `vpc` (plus VPC endpoints), `iam` patterns, `redis`, `s3`, `ses`,
`eventbridge`, `dynamodb`. Mirror anything tests need in
`scripts/create_local_resources.py` (queues, secrets, receipt rule stand-ins).

---

## 12. Build Order (⚠ ADAPTED for actual repo state)

**Step 0 — Prerequisite check:** Core suite green on this machine. Decide the
Phase 2/3 ordering: if Invoicing hasn't built the RDS foundation, this
service's Step 2 includes it (and Invoicing later reuses it).

**Step 1 — Core unfreeze (one deliberate change):** `core/secrets.py` +
`core/realtime.py` + new client factories in `core/clients.py` + new
`Settings` fields + `RATE_LIMITS` entries + root CLAUDE.md module table rows +
any new `ActionType`/error classes. Full Core suite + coverage bar green.
Re-freeze.

**Step 2 — Data layer:** deps (`sqlalchemy[asyncio]`, `asyncpg`, `alembic`,
promote `httpx`), `infra/modules/rds/` if needed, `omnichannel` schema,
Alembic baseline with all tables/indexes/the unique constraint. Postgres
added to `docker-compose.yml` for local/integration tests.

**Step 3 — Adapter contract + Email adapter first** (least new surface;
validates the pattern against `core.email`).

**Step 4 — SMS adapter, then WhatsApp adapter** (webhook Lambda, normalize,
send, delivery interpretation; credentials via `core.secrets`).

**Step 5 — End-to-end message flow:** webhook → SQS → worker → persistence →
`core.events` fan-out → dedupe/retry tests.

**Step 6 — Routing + presence.** **Step 7 — Real-time inbox** via
`core.realtime`. **Step 8 — Commission** (synthetic `invoice.paid` fixture
until Phase 2 exists — §0). **Step 9 — AI features** with the classification
cache. **Step 10 — Load + integration tests, alarms, freeze.**

---

## 13. Out of Scope (unchanged)

Instagram/Messenger (v1.1 — two more adapter files), voice/Connect (v1.5),
skill-based routing/SLA (v2), split commissions (v1.5 — schema already
allows), public Inbox API (post-launch), Permissions service (never — inline
role checks).

---

## 14. Open Decisions — record in `docs/omnichannel-decisions.md` before Step 2

1. Multi-org agents in the UI: simultaneous or context-switch? (Core's
   membership model supports both; this is UI/UX.)
2. Auto-link new WhatsApp numbers to matching clients, or agent-confirmed merge?
3. Per-org SES domain verification at signup vs. 30-day shared-domain grace
   (note: `core/email.py` currently falls back to a default domain when the
   org hasn't verified one — the grace-period behavior is half-implemented
   already; make it a real decision).
4. Voice transcription budget shape (v1.5, but leave pricing-tier room).
5. Public Inbox API day one vs. later.
6. Pricing tier shape ($49–79/mo + pass-through) — Settings/Billing need the
   shape, not this service's code.

---

## 15. Definition of Done

- [ ] `core/secrets.py` + `core/realtime.py` built to Core's bar, module table
      updated, full Core suite green (no regressions), Core re-frozen.
- [ ] `sqlalchemy[asyncio]`/`asyncpg`/`alembic` added once; `httpx` promoted
      to runtime; no non-conforming deps (no Sentry, no aioboto3, no
      aws-secretsmanager-caching).
- [ ] `omnichannel` Postgres schema with all tables + indexes + the
      `(channel_type, external_message_id)` unique constraint.
- [ ] Email/SMS/WhatsApp adapters implement `ChannelAdapter`; each swappable
      and testable in isolation; email goes through `core.email` only.
- [ ] Inbound/outbound flows match docx §7–§8; webhook-retry/duplicate tests
      pass; all rate limits read from `app/config.py::RATE_LIMITS`.
- [ ] Routing + Redis presence working; AppSync real-time via `core.realtime`
      with idle-tab backpressure.
- [ ] Commission snapshots at invoice-creation; `invoice.paid` subscriber
      tested (synthetic event until Phase 2); refund reversal path tested.
- [ ] All §8 cost mitigations implemented, not deferred.
- [ ] CloudWatch logs/X-Ray/`A2Z/OmniChannel` metrics/alarms live; no Sentry.
- [ ] Terragrunt modules applied following the existing `modules/` + `live/`
      layout; local resources mirrored in `scripts/create_local_resources.py`.
- [ ] `ruff` + `mypy --strict` clean; unit + integration + load green; >90%
      coverage on the service package; cross-org isolation proven per table;
      `docs/events.md` updated with every new event type.
- [ ] §14 decisions recorded in `docs/omnichannel-decisions.md`.

---

## 16. Pointers

- **Product behavior:** `OmniChannel_Service_Summary.docx` (external) and the
  original service plan.
- **Core contracts:** root `CLAUDE.md` + `A2Z_Core_Design_TestPlan.md`.
- **Phase 2 foundation shared with this service:** `docs/phase2-invoicing.md`.

If something is ambiguous: reuse Core, cache in the shared Redis, AWS-native,
traceable in CloudWatch, org-scoped, typed, tested, audited.
