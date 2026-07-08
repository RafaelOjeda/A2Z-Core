# A2Z Omni-Channel — Service Context & Build Plan (Adapted for A2Z-Core)

> **Read this first.** This file is the self-contained context for building the
> **Omni-Channel service** inside A2Z-Core: what the product is (§1–§5), how it
> wires into Core (§6), and how to build it with the libraries this repo
> already uses (§7+). It was adapted from an external service plan
> (`OmniChannel_CLAUDE.md` + `OmniChannel_Service_Summary.docx`); where the
> original assumed things that don't match this repo, the correction is marked
> **⚠ ADAPTED**. Product details summarized from the docx are marked
> **(validate against docx)** — confirm them against the source before locking
> the schema, but this file is complete enough to build from.
>
> Root `CLAUDE.md` (Core conventions) still applies in full. Core is frozen:
> anything this service needs from Core is a **deliberate Core change** —
> re-run the entire Core suite (74 tests, >90% coverage bar, `ruff` +
> `mypy --strict`) before continuing (root CLAUDE.md §13, Phase 2 rule).

---

# PART I — WHAT OMNI-CHANNEL IS

## 1. The Product in One Paragraph

Small businesses talk to their customers everywhere at once: a WhatsApp
message about an order, an SMS asking for a quote, an email with a photo
attached. Today those live in three different apps on someone's phone.
**Omni-Channel is a unified inbox**: every customer message, from every
channel, lands in one conversation view per org. Team members ("agents")
claim or get assigned conversations, reply from the same screen — the reply
goes back out through whichever channel the customer used — and when a
conversation leads to a paid invoice (via the Invoicing service), the agent
who handled it is credited commission. It is the second service on the A2Z
platform, and the second proof that Core generalizes.

## 2. Core Concepts (the domain vocabulary)

| Concept | Meaning |
|---|---|
| **Channel** | A communication medium: WhatsApp, SMS, or email at launch (Instagram/Messenger v1.1, voice v1.5). Each org connects its own channel accounts (its WhatsApp Business number, its SMS number, its email domain). |
| **Channel connection** | An org's live link to one channel: credentials, the org's number/address on that channel, and connection status. One org can have several (e.g. two WhatsApp numbers). |
| **Channel identity** | A *customer's* handle on a channel — a phone number, an email address. Identities can be linked to one customer across channels (the same person's phone + email). |
| **Conversation** | The org-scoped thread with one customer. Messages from any of that customer's linked identities collapse into one conversation. Has a status (`open`, `pending`, `closed`) and at most one current assignee. |
| **Message** | One inbound or outbound item in a conversation: text, media attachments, delivery status, the channel it traveled on, and the provider's `external_message_id` (the idempotency key). |
| **Assignment** | Who owns a conversation right now. Assignment history is **append-only** — every claim, auto-route, and reassignment is a new row, never an update. This history is what makes commission replayable. |
| **Presence** | Whether an agent is online/away/offline right now. Live state in Redis; routing only auto-assigns to online agents. |
| **Routing strategy** | The org-configurable rule for who gets a new conversation: round-robin, sticky, or single-assignee (§5.3). |
| **Commission attribution** | The record that agent X earns commission on invoice Y because they were assigned to the conversation when the invoice was created (§5.5). |
| **Template** | A saved reply (required by WhatsApp for business-initiated messages outside the 24h window; convenient everywhere else). |

## 3. User Flows (what the product does, end to end)

**Inbound — customer writes in:**
A customer sends a WhatsApp message to the org's business number. Meta calls
our webhook. We verify the signature, drop duplicates, and enqueue the raw
payload. The worker normalizes it into a `Message`, finds-or-creates the
`Conversation` for that customer identity, stores media in S3, runs the
routing strategy to pick an assignee, and pushes a real-time update so the
inbox refreshes on every connected agent's screen — all within a couple of
seconds. The assigned agent gets a notification.

**Reply — agent responds:**
The agent types a reply in the unified inbox (or picks a template). The API
enqueues an outbound job; the worker sends it through the right channel
adapter (WhatsApp Graph API / SMS provider / `core.email`), records the
message with its provider ID, and later processes the delivery webhook
(sent → delivered → read) to update message status live in the UI.

**Conversation → invoice → commission:**
Mid-conversation the customer agrees to buy. The agent clicks "create
invoice"; Omni-Channel publishes `conversation.invoice_requested` and the
Invoicing service creates a draft, snapshotting *this agent* as the
attribution. Days later the customer pays; Invoicing publishes
`invoice.paid`; Omni-Channel's subscriber credits the commission to the
snapshotted agent — even if the conversation was reassigned in between.

**Team management:**
Owners/admins connect channels (enter WhatsApp credentials, verify the email
domain, provision the SMS number), set the routing strategy, define
commission rules, and see a dashboard (response times, volume per channel,
commission per agent). Agents see their own inbox and their commission tally.

## 4. Roles & Permissions

Roles come from `core.membership` (Core stores the string; this service
interprets it — same convention as everywhere else, root CLAUDE.md §14).
Checks are inline `role in {...}` in handlers; there is no Permissions service.

| Capability | Owner | Admin | Agent | Viewer |
|---|:-:|:-:|:-:|:-:|
| Read all org conversations | ✓ | ✓ | ✓ | ✓ |
| Reply / send outbound | ✓ | ✓ | ✓ | — |
| Claim / self-assign a conversation | ✓ | ✓ | ✓ | — |
| Reassign someone else's conversation | ✓ | ✓ | — | — |
| Connect/disconnect channels, edit credentials | ✓ | ✓ | — | — |
| Configure routing strategy & templates | ✓ | ✓ | — | — |
| Define commission rules | ✓ | — | — | — |
| See all agents' commission | ✓ | ✓ | own only | — |

*(validate against docx §3 — the Agent/Viewer boundaries especially)*

## 5. Service Design

### 5.1 Data Model — Postgres, `omnichannel` schema

Relational shape (threads, append-only histories, joins for the dashboard) is
why this is **Postgres, not DynamoDB** — deliberate decision carried from the
original plan. Lives in the **shared RDS instance** in a dedicated
`omnichannel` schema; never a second instance (cost principle). Every table
carries `org_id` and every query filters on it (golden rule #2).

| Table | Purpose / key columns *(validate against docx §6)* |
|---|---|
| `channel_connections` | org's live channel links: `org_id`, `channel_type`, `display_name`, `provider_account_id` (phone-number ID, sending address), `credentials_secret_key` (→ Secrets Manager name, **never** the secret itself), `status`, timestamps. |
| `channel_identities` | customer handles: `org_id`, `channel_type`, `external_id` (E.164 phone / email addr), `display_name`, `customer_id` (nullable link for cross-channel merge), unique `(org_id, channel_type, external_id)`. |
| `conversations` | `org_id`, `customer_identity_id`, `status` (`open`/`pending`/`closed`), `assigned_user_id` (current, denormalized for the hot inbox query), `last_message_at`, `last_message_preview`, `unread_count`. |
| `messages` | `org_id`, `conversation_id`, `direction` (`inbound`/`outbound`), `channel_type`, `external_message_id`, `body_text`, `content_type`, `status` (`received`/`queued`/`sent`/`delivered`/`read`/`failed`), `sent_by_user_id` (outbound), `created_at`. **Unique `(channel_type, external_message_id)` — the webhook-idempotency guarantee. Do not skip it.** |
| `message_attachments` | `message_id`, `org_id`, `s3_key` (via `core.storage`, `{org_id}/omnichannel/...`), `content_type`, `size_bytes`. |
| `conversation_assignments` | **append-only**: `org_id`, `conversation_id`, `assigned_user_id`, `assigned_by` (user id or `"routing:round_robin"`), `reason`, `created_at`. Never updated, never deleted. |
| `presence` | backup/audit row per agent (`org_id`, `user_id`, `status`, `updated_at`). **Live state is Redis** (§5.3); this table is not the hot path. |
| `templates` | saved replies: `org_id`, `name`, `channel_type` (nullable = any), `body`, `variables`, `provider_template_id` (WhatsApp-approved templates). |
| `commission_rules` | **append-only history**: `org_id`, `percent` (or flat amount), `effective_from`, `created_by`. Current rule = latest row; changing the rule inserts, never mutates. |
| `commission_attributions` | `org_id`, `invoice_id`, `conversation_id`, `agent_user_id`, `rule_snapshot` (percent at attribution time), `amount` (filled on payment), `status` (`pending`/`credited`/`reversed`), timestamps. Derived/replayable from assignments + invoices. |

Indexes at table-creation time, not later: inbox query
(`org_id, status, last_message_at DESC`), agent inbox (`org_id,
assigned_user_id, status`), thread view (`conversation_id, created_at`),
full-text GIN on `messages.body_text` (tsvector), agent dashboard
(`org_id, agent_user_id` on attributions).

### 5.2 Channel Adapters — one file per channel, one contract

Everything channel-specific lives behind one Protocol (§7 has the code). The
rest of the system — worker, routing, inbox — never knows which channel it's
touching. Adding Instagram later = one new file + one registry entry.

**Email adapter — wire to Core, don't bypass it.** `send_outbound` calls
`core.email.send_email(org_id, service_type=ServiceType.OMNICHANNEL, ...)` —
never boto3 SES directly. That buys, for free: suppression checking, the
50/hr/org rate limit, per-org config-set isolation, audit logging, and
delivery-status events, all already built and tested in Core. **Inbound**
email is service-owned (Core doesn't do inbound): SES receipt rule → S3 →
Lambda reads the raw MIME via `core.storage`, parses with stdlib `email`,
and feeds the same `normalize_inbound` path as any other channel.

**WhatsApp adapter:** Meta WhatsApp Cloud API (Graph API) over `httpx`.
Inbound webhook verification = HMAC SHA-256 of the raw body against the app
secret (`X-Hub-Signature-256`). Credentials (access token, phone-number ID,
app secret) per org via `core.secrets`. Business-initiated messages outside
the 24-hour customer-service window must use approved templates — surface
that as a `SupportedFeatures`/adapter concern, not scattered `if`s.

**SMS adapter:** provider API (e.g. SNS or Twilio-style — pick at build
time) over `httpx`; delivery receipts via webhook; 10DLC registration is an
infra/onboarding prerequisite, not code.

### 5.3 Routing & Presence

Three strategies at launch, org-configurable:

- **Round-robin** — new conversation goes to the *online* agent who has
  waited longest since their last auto-assignment. Skips offline/away agents;
  if nobody is online, the conversation stays unassigned in the shared inbox.
- **Sticky** — returning customer goes back to the agent who last responded
  to them, if that agent is online; falls back to round-robin otherwise.
- **Single-assignee** — everything goes to one designated user (solo
  businesses; the owner *is* the inbox).

`presence.py` keeps live status in Redis (shared cluster, keys
`presence:{org_id}:{user_id}`, heartbeat TTL ~60s so a closed laptop decays
to offline). The Postgres `presence` row is a backup/audit write, not read on
the hot path. Every routing decision writes a `conversation_assignments` row
and `core.audit.log_audit`.

### 5.4 Real-Time Inbox

Agents' inboxes update live — new message, assignment change, delivery tick —
via AppSync GraphQL subscriptions through the new `core.realtime.publish_update`
(§6.2). Auth re-checks org membership on subscribe: a revoked membership must
terminate the subscription on the next reconnect. Idle tabs (>5 min in
background) drop to long-poll fallback — AppSync connection-minutes are a
named cost vector (§10).

### 5.5 Commission Attribution — the load-bearing business rule

**Snapshot the assigned agent at invoice-creation time, not payment time.**
The agent who did the selling gets the credit, even if the conversation is
later reassigned or the payment arrives weeks later. Do not "simplify" this
to payment-time attribution — that is the one rule the whole feature exists
to get right.

Mechanics: on `conversation.invoice_requested` → invoice created, write a
`commission_attributions` row with the conversation's `assigned_user_id` and
the *current* commission rule snapshotted in. On `invoice.paid` (event from
Invoicing), fill `amount` and set `status='credited'`. On refund, set
`status='reversed'` — never delete. Because `conversation_assignments` is
append-only and rules are history rows, attributions are fully replayable
from source data.

### 5.6 Message Flow — the highest-stakes hot path

Webhook providers retry aggressively (Meta ~10s window); at-least-once
delivery is the norm everywhere. Done wrong, this produces duplicate
customer-visible messages. The pipeline, in order:

**Inbound:** webhook Lambda: verify signature → ack fast (<2s p99, just
validate + enqueue to the channel's SQS queue) ⇒ worker: dedupe on
`(channel_type, external_message_id)` (insert, treat unique-violation as
already-processed no-op) → normalize via adapter → find-or-create identity +
conversation → persist message + attachments (media to S3 via `core.storage`)
→ run routing if unassigned → `core.events.publish_event("message.received")`
→ `core.realtime.publish_update` → notify assignee.

**Outbound (mirrored):** API handler: authz via `core.membership` → rate
limit via `core.rate_limit` → persist as `queued` → enqueue to outbound SQS ⇒
worker: adapter `send_outbound` (credentials via `core.secrets`) → store
`external_message_id`, mark `sent` → publish `message.sent` + realtime update
⇒ later, delivery webhook → adapter `interpret_delivery_webhook` → status
updates (`delivered`/`read`/`failed`) → realtime update. Failed sends: bounded
retry with backoff, then DLQ + alarm — never blind infinite retry (SMS costs
real money per attempt).

---

# PART II — HOW IT WIRES INTO A2Z-CORE (⚠ ADAPTED)

## 6. Reality Check & Core Dependency Map

### 6.0 What exists in this repo today (⚠ ADAPTED)

The original plan assumed Invoicing (Phase 2) was already built. It is not:
`app/services/invoicing/` is an empty stub; only `docs/phase2-invoicing.md`
exists. Consequences:

1. **The shared Postgres foundation does not exist yet** — no
   `infra/modules/rds/`, no SQLAlchemy/asyncpg/alembic in `pyproject.toml`.
   Whichever phase starts first builds it (per `docs/phase2-invoicing.md`
   step 1); the other reuses it (shared instance, separate schema).
2. **`invoice.paid` has no real producer yet.** Build the commission
   subscriber against the documented event shape with a synthetic event in
   tests; wire the real producer when Phase 2 lands.
3. Core already anticipated this service: `ServiceType.OMNICHANNEL` exists in
   `core/email.py`, `RATE_LIMITS` in `app/config.py` is ready for new
   actions, and `core.events.publish_event` takes `source="a2z.omnichannel"`.

### 6.1 Existing Core modules (call these; never reimplement)

| Core API (as implemented in this repo) | Used for |
|---|---|
| `core.auth.get_current_user_from_request(request)` / `validate_jwt(token)` | JWT on every request; tests use `core.auth.create_test_token(...)`. |
| `core.membership.get_membership(user_id, org_id) -> Membership \| None` | Role checks per §4, inline in handlers. |
| `core.email.send_email(org_id, service_type=ServiceType.OMNICHANNEL, ...)` | All outbound email (§5.2). |
| `core.storage.upload_file / download_file / generate_signed_url / get_file_metadata` | Media, keys `{org_id}/omnichannel/...` (org prefix enforced by `_assert_org_scope`). |
| `core.audit.log_audit(...)` | Every assignment, reassignment, outbound send, channel connect/disconnect, rule change. Extend `ActionType` if needed (a Core change — §6.2 protocol). |
| `core.settings.get_org_settings(org_id) -> OrgSettings` | Timezone, verified sending domain, sender name, plan tier. Redis-cached already. |
| `core.events.publish_event(org_id, event_type, data, source="a2z.omnichannel") -> str` | Publish `message.received`, `message.sent`, `conversation.assigned`, `conversation.invoice_requested`. Update `docs/events.md` per new type. |
| `core.rate_limit.check_and_increment(org_id, action, limit=..., window_seconds=...)` + `limits_for(action)` | Per-channel outbound limits from `app/config.py::RATE_LIMITS` (§6.4) — never hardcoded literals. |
| `core.exceptions.CoreError` | Base of this service's errors (§8); each carries `status_code`, routers map it — zero new plumbing. |
| `core.logging.get_logger(name)` | Structured JSON logs. Never configure logging yourself. |
| `core.clients` | The **only** place AWS/Redis clients are built. New clients (Secrets Manager, AppSync) are added there as `@lru_cache` factories; every sync boto3 call goes through `await core.clients.run_aws(fn, ...)`. |

### 6.2 NEW Core modules required (promote, don't duplicate)

`core/secrets.py` and `core/realtime.py` are **Core modules**, not
Omni-Channel-private code — the next service gets them for free.
**Unfreeze protocol:** add them to the root `CLAUDE.md` module table, meet
Core's bar (unit + integration tests, >90% coverage, cross-org isolation
test, docstrings with perf targets), re-run the full Core suite green, then
re-freeze Core before writing any Omni-Channel-specific code.

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

> **⚠ ADAPTED:** the original plan mandated the AWS *Secrets Manager Caching
> Client* (`aws-secretsmanager-caching`). It is sync-only, adds a dependency,
> and duplicates the caching idiom Core already has (Redis TTL in
> `core.settings`, JWKS cache in `core.auth`). Use the Redis pattern. On
> rotation: delete the Redis key from the rotation path, or accept the
> ≤5-min staleness window (document the choice).

**`app/core/realtime.py`** — real-time fan-out to connected clients.

```python
async def publish_update(org_id: str, channel: str, payload: dict[str, Any]) -> None:
    """Push a real-time update to connected clients via AppSync.

    - channel examples: f"org:{org_id}:conversations",
      f"conversation:{conversation_id}:messages"
    - Wraps an AppSync GraphQL mutation that fans out to subscribers. The
      HTTP call uses httpx (§7 deps) with IAM SigV4 signing via botocore's
      SigV4Auth — no new signing library.
    - Fire-and-forget from caller's perspective; errors logged, not swallowed.

    Raises: RealtimeError (CoreError, 502) only on config errors.
    Performance: < 100ms.
    """
```

> **Test-harness note (⚠ ADAPTED):** Core's integration tests run on **moto +
> fakeredis**, not LocalStack. moto covers Secrets Manager (add the extra)
> but has no usable AppSync data plane — unit-test `core.realtime` against a
> stubbed transport (httpx `MockTransport`) and flag real-AWS verification
> explicitly in the test plan; don't skip it silently.

### 6.3 Cross-service rule (non-negotiable)

Omni-Channel and Invoicing never import each other's code. Events only:
Omni-Channel publishes `conversation.invoice_requested` → Invoicing consumes;
Invoicing publishes `invoice.paid` → Omni-Channel consumes (§5.5).

**⚠ ADAPTED:** Core owns only the *publisher*. Subscribers are service-owned:
an EventBridge **rule** on `a2z-bus` (`source = a2z.invoicing`,
`detail-type = invoice.paid`) targets this service's **events SQS queue**;
the Fargate worker consumes it. Rule + queue live in this service's
Terragrunt modules (§12).

### 6.4 Rate-limit registry additions (`app/config.py`)

```python
RATE_LIMITS: dict[str, tuple[int, int]] = {
    # ... existing entries ...
    "omnichannel.whatsapp.send": (80, 1),      # Meta pair-rate ceiling; tune per tier
    "omnichannel.sms.send": (60, 60),          # provider throughput cap per org
    "omnichannel.ai.classify": (500, 86400),   # Bedrock cost guard per org
    # email channel needs no new entry: core.email already enforces email.send
}
```

Exact numbers come from the provider contracts at build time; the point is
they live here, config-driven.

---

# PART III — BUILD SPECIFICS

## 7. Stack & Libraries (⚠ ADAPTED — locked to this repo's pyproject)

Python 3.12 + FastAPI, same repo, same modular monolith.

**Already pinned — use these, add nothing that duplicates them:**

| Need | Use (already in `pyproject.toml`) |
|---|---|
| AWS calls | `boto3` (sync) via `core.clients` factories + `await run_aws(...)`. **No aioboto3.** |
| Redis | `redis.asyncio` via `core.clients.redis_client()` — shared client, namespaced keys (`presence:{org_id}:*`, `secret:*`, `aiclass:{content_hash}`, `mediaurl:{key}`). |
| DTOs / validation | `pydantic` v2 — adapter types, models, everything. |
| Config | `pydantic-settings` — new fields on `app/config.py::Settings` with env aliases (`APPSYNC_ENDPOINT`, `DATABASE_URL`, SQS queue URLs). No per-service config module. |
| JWT | `python-jose`, already wrapped by `core.auth`; never used directly here. |
| Webhook signatures | stdlib `hmac` + `hashlib`. No new dependency. |
| Inbound MIME parsing | stdlib `email` package. No new dependency. |
| Lint / types | `ruff` (line-length 100, E,F,I,B,UP,ASYNC) + `mypy --strict` with pydantic plugin — config already in `pyproject.toml`, no per-service overrides. |
| Tests | `pytest` + `pytest-asyncio` (auto mode), `moto`, `fakeredis`, `httpx` — same harness as Core. **Not LocalStack** (docker-compose LocalStack is manual-dev only; CI uses moto). |

**New dependencies this service may add:**

| Dependency | Why | Where |
|---|---|---|
| `sqlalchemy[asyncio]`, `asyncpg`, `alembic` | Postgres data layer — the same deps `docs/phase2-invoicing.md` step 1 earmarks for Invoicing. Add once; both services share. | `[project.dependencies]` |
| `httpx` | Runtime HTTP for WhatsApp Graph API, SMS provider, AppSync mutations. Already a dev dep — promote. | move to `[project.dependencies]` |
| `moto[secretsmanager]`, `boto3-stubs[secretsmanager,sqs]` extras | Test/typing for the new AWS surfaces. | dev extras |

**Explicitly NOT added (⚠ ADAPTED deviations):**

- `aws-secretsmanager-caching` — replaced by the Redis idiom (§6.2).
- **Sentry** — the original plan kept it alongside CloudWatch; that violates
  the AWS-only principle and adds a dep Core doesn't have. CloudWatch Logs
  Insights + alarms + X-Ray cover it. Revisit deliberately only if exception
  grouping proves genuinely missing.
- Any WebSocket library — real-time is AppSync via `core.realtime`, period.
- A second Bedrock/AI client — define it once in `core/clients.py`; whichever
  service ships first creates it, the other inherits.

**Adapter contract (Python, final):**

```python
# app/services/omnichannel/adapters/base.py
from typing import Any, Protocol, runtime_checkable

@runtime_checkable
class ChannelAdapter(Protocol):
    """Every channel implements this. One file per channel. Nothing else in
    the system may know which channel it's talking to beyond this contract."""

    supported_features: "SupportedFeatures"  # templates, rich_media, typing_indicators, read_receipts

    async def verify_inbound_signature(self, raw_body: bytes, headers: dict[str, str], secret: str) -> bool: ...
    async def normalize_inbound(self, raw_payload: dict[str, Any]) -> list["NormalizedInboundMessage"]: ...
    async def send_outbound(self, to: str, content: "OutboundContent", credentials: dict[str, Any]) -> "SendResult": ...
    async def interpret_delivery_webhook(self, raw_payload: dict[str, Any]) -> list["DeliveryStatusUpdate"]: ...
```

Shared types are Pydantic models in `adapters/types.py`. Adding a channel =
one new file + one registry entry in `adapters/registry.py`. Nothing else
changes.

## 8. Errors (wired to Core's hierarchy)

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

Each sets `status_code` exactly as Core's errors do. `RateLimitError`,
`SuppressionListError`, etc. are raised *by Core* and pass through untouched.

## 9. Repo Layout (extends what's already here)

```
A2Z-Core/
├── app/
│   ├── core/                          # frozen; unfrozen once for:
│   │   ├── secrets.py                 # ←★ NEW (§6.2)
│   │   └── realtime.py                # ←★ NEW (§6.2)
│   ├── services/omnichannel/          # ←★ THIS SERVICE (stub exists)
│   │   ├── models.py                  # Pydantic DTOs + SQLAlchemy tables (§5.1)
│   │   ├── db.py                      # async engine/session, "omnichannel" schema
│   │   ├── adapters/                  # base.py, types.py, whatsapp.py, sms.py, email.py, registry.py
│   │   ├── routing.py                 # §5.3 strategies
│   │   ├── presence.py                # Redis-backed presence
│   │   ├── commission.py              # §5.5 attribution + invoice.paid handler
│   │   ├── handlers.py                # business logic called by routers
│   │   ├── webhooks/                  # whatsapp_webhook.py, sms_webhook.py, ses_inbound.py
│   │   └── worker.py                  # Fargate worker: SQS consumer (§5.6)
│   ├── routers/omnichannel.py         # thin HTTP layer, mounted in app/main.py
│   └── lambdas/                       # thin entrypoints → services/omnichannel/webhooks
│       ├── omnichannel_whatsapp_webhook.py
│       ├── omnichannel_sms_webhook.py
│       └── omnichannel_ses_inbound.py
├── infra/
│   ├── modules/                       # NEW: rds (if Phase 2 hasn't built it), appsync,
│   │   ...                            #      secretsmanager-channels, sqs-omnichannel,
│   │                                  #      ses-receipt-rules, omnichannel-worker, lambda-webhooks
│   └── live/prod/<name>/terragrunt.hcl   # one live folder per module — existing layout
└── tests/{unit,integration,load}/omnichannel/
```

**Reused, not re-provisioned:** the RDS instance (new schema), the Redis
cluster (new namespaces), the SES sending pipeline (via `core.email`), Core's
VPC/IAM modules. Alembic migrations live with the service; DynamoDB backfill
scripts stay in `infra/migrations/` per root CLAUDE.md §9.

## 10. Cost & Caching (all via the shared Redis client)

Every cache uses `core.clients.redis_client()` with a namespaced key — no
second cache system:

| Cost vector | Mitigation |
|---|---|
| Bedrock calls | `aiclass:{content_hash}`, 24h TTL; suggested replies on explicit click only, never per-message |
| SES volume | batch notifications ("3 new messages"), org's own verified domain (Core config sets already per org/service) |
| SMS | 10DLC upfront; sticky-route to same number per customer; backoff, never blind retry |
| AppSync minutes | idle tabs → long-poll after 5 min |
| RDS IO | messages >12 months to cold partition; hot inbox queries never touch archive |
| S3 egress | `mediaurl:{key}` signed-URL cache, 1h TTL (< signed expiry) |
| NAT Gateway | VPC endpoints for S3, SQS, Secrets Manager, SES, Bedrock — extend the existing `vpc` module; the silent-killer line item |
| Secrets Manager API | 5-min Redis TTL in `core.secrets` (§6.2) |

Reusing shared RDS + Redis avoids a second ~$15–25/mo instance and ~$12/mo
cluster — the single biggest DRY-driven cost win here. Estimated fixed spend
at MVP (~50 orgs): ≈$130/mo; SES/SMS usage passes through to customers.

## 11. Observability (CloudWatch only — ⚠ ADAPTED: no Sentry)

- Structured JSON logs via `core.logging` with `request_id` /
  `conversation_id` / `message_id` threaded through every line of a flow.
- X-Ray on all Lambdas + worker/API; webhook → SQS → worker → EventBridge →
  AppSync traceable as one trace.
- Namespace `A2Z/OmniChannel`: `WebhookAckLatencyMs` per channel (alarm p99
  > 2s — Meta's retry window is ~10s), `MessageProcessingLatencyMs` (receipt
  → visible in inbox), `RoutingLatencyMs`, `SendSuccessRate`/`SendFailureRate`
  per channel, `AICacheHitRate`, `ActiveAppSyncConnections`.
- Alarms: any DLQ depth > 0, webhook ack p99 breach, send failure rate > 5%.

## 12. Terragrunt (follow the existing `infra/` layout exactly)

New `infra/modules/`: `rds/` (only if Phase 2 hasn't built it), `appsync/`,
`secretsmanager-channels/`, `sqs-omnichannel/` (inbound-per-channel +
outbound + events queues, DLQs on all, plus the EventBridge rule targeting
the events queue, §6.3), `ses-receipt-rules/`, `omnichannel-worker/`,
`lambda-webhooks/`. Each gets a matching `infra/live/prod/<name>/terragrunt.hcl`
like the nine existing ones. Reused unmodified: `vpc` (+ endpoints), `iam`,
`redis`, `s3`, `ses`, `eventbridge`, `dynamodb`. Mirror what tests need in
`scripts/create_local_resources.py`.

## 13. Build Order (⚠ ADAPTED for actual repo state)

**Step 0 — Prerequisite check:** Core suite green locally. Decide Phase 2/3
ordering: if Invoicing hasn't built the RDS foundation, Step 2 here includes
it (Invoicing reuses it later).

**Step 1 — Core unfreeze (one deliberate change):** `core/secrets.py` +
`core/realtime.py` + client factories in `core/clients.py` + `Settings`
fields + `RATE_LIMITS` entries + root CLAUDE.md module-table rows + any new
`ActionType`/error classes. Full Core suite + coverage bar green. Re-freeze.

**Step 2 — Data layer:** deps (`sqlalchemy[asyncio]`, `asyncpg`, `alembic`,
promote `httpx`), `infra/modules/rds/` if needed, `omnichannel` schema,
Alembic baseline with all tables/indexes/the unique constraint (§5.1).
Postgres added to `docker-compose.yml` for local/integration tests. Record
§14 decisions in `docs/omnichannel-decisions.md` first.

**Step 3 — Adapter contract + Email adapter first** — reuses `core.email`
almost entirely, so it validates the adapter pattern with the least new
surface area.

**Step 4 — SMS adapter, then WhatsApp adapter** — each: webhook Lambda,
normalize, send, delivery interpretation, credentials via `core.secrets`.

**Step 5 — End-to-end message flow (§5.6):** webhook → SQS → worker →
persistence → `core.events` fan-out; webhook-retry/duplicate tests.

**Step 6 — Routing + presence (§5.3).**
**Step 7 — Real-time inbox (§5.4)** via `core.realtime`, idle-tab backpressure.
**Step 8 — Commission (§5.5)** — synthetic `invoice.paid` fixture until
Phase 2 exists (§6.0).
**Step 9 — AI features** — on-demand summary + suggested reply via Bedrock,
with the classification cache (§10).
**Step 10 — Load + integration tests,** latency targets from §11, DLQ/alarm
wiring, then freeze.

## 14. Open Decisions — record in `docs/omnichannel-decisions.md` before Step 2

1. Multi-org agents in the UI: simultaneous or context-switch? (Core's
   membership model supports both; this is UI/UX.)
2. Auto-link a new WhatsApp number to a client with a matching phone, or
   agent-confirmed merge? (Affects `channel_identities.customer_id` writes.)
3. Per-org SES domain verification at signup vs. a 30-day shared-domain
   grace period. Note: `core/email.py` currently falls back to a default
   domain when the org hasn't verified one — the grace behavior is
   half-implemented already; make it a real decision.
4. Voice transcription budget shape (v1.5, but leave pricing-tier room).
5. Public Inbox API day one vs. post-launch.
6. Pricing tier shape ($49–79/mo + SMS/WhatsApp pass-through) —
   Settings/Billing need the shape, not this service's code.

## 15. Out of Scope (don't add now)

Instagram DM / Messenger (v1.1 — two more adapter files, nothing else
changes), voice/Amazon Connect (v1.5), skill-based routing & SLA escalation
(v2), split commissions (v1.5 — schema already allows multiple attributions
per invoice), public Inbox API (post-launch), a Permissions service (never —
inline role checks).

## 16. Definition of Done

- [ ] `core/secrets.py` + `core/realtime.py` built to Core's bar, module
      table updated, full Core suite green (no regressions), Core re-frozen.
- [ ] `sqlalchemy[asyncio]`/`asyncpg`/`alembic` added once; `httpx` promoted;
      no non-conforming deps (no Sentry, no aioboto3, no
      aws-secretsmanager-caching).
- [ ] `omnichannel` Postgres schema with all §5.1 tables + indexes + the
      `(channel_type, external_message_id)` unique constraint.
- [ ] Email/SMS/WhatsApp adapters implement `ChannelAdapter`; each swappable
      and testable in isolation; email goes through `core.email` only.
- [ ] Inbound/outbound flows match §5.6; webhook-retry/duplicate tests pass;
      all rate limits read from `app/config.py::RATE_LIMITS`.
- [ ] Routing (round-robin, sticky, single-assignee) + Redis presence
      working; AppSync real-time via `core.realtime` with idle-tab
      backpressure.
- [ ] Commission snapshots at invoice-creation; `invoice.paid` subscriber
      tested (synthetic event until Phase 2); refund reversal path tested.
- [ ] All §10 cost mitigations implemented, not deferred.
- [ ] CloudWatch logs / X-Ray / `A2Z/OmniChannel` metrics / alarms live.
- [ ] Terragrunt modules applied per the existing layout; local resources
      mirrored in `scripts/create_local_resources.py`.
- [ ] `ruff` + `mypy --strict` clean; unit + integration + load green; >90%
      coverage on the service package; cross-org isolation proven per table;
      `docs/events.md` updated with every new event type.
- [ ] §14 decisions recorded, and every **(validate against docx)** marker in
      this file confirmed or corrected against `OmniChannel_Service_Summary.docx`.

## 17. Pointers

- **Product source of truth for anything marked (validate against docx):**
  `OmniChannel_Service_Summary.docx` (external, not in repo).
- **Core contracts:** root `CLAUDE.md` + `A2Z_Core_Design_TestPlan.md`.
- **Shared Phase 2 foundation:** `docs/phase2-invoicing.md`.

If something is ambiguous: reuse Core, cache in the shared Redis, AWS-native,
traceable in CloudWatch, org-scoped, typed, tested, audited.
