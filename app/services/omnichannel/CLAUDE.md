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
>
> **Deployment revision (2026-07-12):** MVP runs on a **single EC2 instance**
> (§12) — API + worker processes plus Postgres and Redis containers on one
> box. The distributed shape (Fargate, RDS, ElastiCache, ALB, NAT, AppSync)
> is a deliberate *later* phase; the seams that make that split cheap (SQS,
> the `core.realtime` facade, Secrets Manager) are kept from day one. Where
> older text below assumed Fargate/Lambda/AppSync, §12 supersedes it.
>
> **Scope revision (2026-07-12):** v1 is the *minimal* working product —
> AI/Bedrock features are **cut**, and auto-routing/presence, templates,
> commission, and dashboards are **deferred** (§15). v1 = a multi-tenant
> unified inbox: companies connect their channels, messages arrive, agents
> claim and reply. §1.1 states the tenancy model explicitly.
>
> **Channel scope revision (2026-07-12):** **SMS is cut from v1.** v1
> channels are WhatsApp and email only. The `ChannelAdapter` contract (§5.2,
> §7) means adding SMS later is one new adapter file + a registry entry —
> no other code or infra changes.

---

# PART I — WHAT OMNI-CHANNEL IS

## 1. The Product in One Paragraph

Small businesses talk to their customers everywhere at once: a WhatsApp
message about an order, an email with a photo attached, later an SMS asking
for a quote. Today those live in different apps on someone's phone.
**Omni-Channel is a unified inbox**: every customer message, from every
channel, lands in one conversation view per org. Team members ("agents")
claim or get assigned conversations, reply from the same screen — the reply
goes back out through whichever channel the customer used — and when a
conversation leads to a paid invoice (via the Invoicing service), the agent
who handled it is credited commission. It is the second service on the A2Z
platform, and the second proof that Core generalizes.

## 1.1 Multi-Tenancy — companies, users, isolation (explicit)

This service is multi-tenant by construction: a **company = an org**, Core's
tenancy unit. Nothing here is single-company. Concretely:

- **Many companies, one deployment.** Tenancy is row-level, not per-tenant
  infrastructure: the one EC2/Postgres/Redis (§12) serves every org; every
  table carries `org_id` (§5.1) and every query filters on it (root golden
  rule #2).
- **Each company connects its own channels.** `channel_connections` rows are
  org-scoped, and credentials live per org in Secrets Manager
  (`a2z/{org_id}/omnichannel/...`, §6.2). Two companies can connect the same
  channel type — even the same provider — without ever seeing each other's
  traffic.
- **Inbound messages resolve to the right company via the connection that
  received them:** every channel connection registers its own webhook URL
  (`POST /webhooks/{channel_type}/{connection_id}`, §5.6), so the
  `connection_id` yields the org and its signing secret before anything else
  runs; inbound email resolves the org from the recipient address. A
  conversation belongs to exactly one org — there is no cross-org inbox.
- **Users belong to companies via `core.membership`** (already built and
  tested in Core): roles are per-org (§4), one user may belong to several
  orgs, and agents only ever see conversations for orgs they are members of
  — membership is re-checked on every API call and on realtime connects
  (§5.4).
- **Isolation is tested, not assumed:** the DoD (§16) requires a cross-org
  isolation test per table, the same bar Core met.

## 2. Core Concepts (the domain vocabulary)

| Concept | Meaning |
|---|---|
| **Channel** | A communication medium: WhatsApp or email at launch (SMS deferred — see channel scope revision above; Instagram/Messenger v1.1, voice v1.5). Each org connects its own channel accounts (its WhatsApp Business number, its email domain). |
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
adapter (WhatsApp Graph API / `core.email`), records the
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
domain), set the routing strategy, define
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
original plan. Lives in the **shared Postgres instance** (a container on the
EC2 at MVP, RDS at distribution — §12) in a dedicated `omnichannel` schema;
never a second instance (cost principle). Every table
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

**`channel_type` is `TEXT`, never a Postgres `ENUM`** — validated by a
Pydantic enum at the app layer only. A DB enum would force a migration
across six tables for every new channel; adding a channel must never
require a schema change.

### 5.2 Channel Adapters — one file per channel, one contract

Everything channel-specific lives behind one Protocol (§7 has the code). The
rest of the system — worker, routing, inbox — never knows which channel it's
touching. Adding Instagram later = one new file + one registry entry.

**Three invariants protect that promise** (violating any of them silently
turns "add a channel" into a migration + infra project): `channel_type` is
`TEXT` in Postgres (§5.1), all inbound webhooks share one generic route
(§5.6), and all channels share one inbound SQS queue (§5.6). A new channel
touches `adapters/` and the registry — nothing else, including infra.

**Email adapter — wire to Core, don't bypass it.** `send_outbound` calls
`core.email.send_email(org_id, service_type=ServiceType.OMNICHANNEL, ...)` —
never boto3 SES directly. That buys, for free: suppression checking, the
50/hr/org rate limit, per-org config-set isolation, audit logging, and
delivery-status events, all already built and tested in Core. **Inbound**
email is service-owned (Core doesn't do inbound): SES receipt rule → S3 →
S3 event notification onto the shared inbound SQS queue
(`channel_type=email`, §5.6) → the worker reads the raw MIME via
`core.storage`, parses with stdlib `email`, resolves the org from the
recipient address via `channel_connections`, and feeds the same
`normalize_inbound` path as any other channel. No Lambda needed.

**WhatsApp adapter:** Meta WhatsApp Cloud API (Graph API) over `httpx`.
Inbound webhook verification = HMAC SHA-256 of the raw body against the app
secret (`X-Hub-Signature-256`). Credentials (access token, phone-number ID,
app secret) per org via `core.secrets`. Business-initiated messages outside
the 24-hour customer-service window must use approved templates — surface
that as a `SupportedFeatures`/adapter concern, not scattered `if`s. (v1
ships without templates, so v1 WhatsApp is reply-within-24h only — the
accepted consequence is recorded in §15.)

**SMS adapter — deferred (§15).** Not built for v1. When added: provider API
(e.g. SNS or Twilio-style — pick at build time) over `httpx`; delivery
receipts via webhook; 10DLC registration is an infra/onboarding
prerequisite, not code. Nothing else in the system changes to add it — that
is the point of the adapter contract (§5.2, §7).

### 5.3 Routing & Presence

**v1 (minimal): manual claim + single-assignee only.** New conversations
land unassigned in the shared inbox (or go to the designated user when
single-assignee is configured); agents claim them. No presence system
needed. The auto-strategies below are **deferred** (§15) — design kept for
when they land:

- **Round-robin** — new conversation goes to the *online* agent who has
  waited longest since their last auto-assignment. Skips offline/away agents;
  if nobody is online, the conversation stays unassigned in the shared inbox.
- **Sticky** — returning customer goes back to the agent who last responded
  to them, if that agent is online; falls back to round-robin otherwise.
- **Single-assignee** — everything goes to one designated user (solo
  businesses; the owner *is* the inbox).

*(Deferred with auto-routing)* `presence.py` keeps live status in Redis
(keys `presence:{org_id}:{user_id}`, heartbeat TTL ~60s so a closed laptop
decays to offline); the Postgres `presence` row is a backup/audit write, not
read on the hot path. **Built in v1 regardless:** every assignment (claim,
reassign, single-assignee) writes an append-only `conversation_assignments`
row and `core.audit.log_audit` — that history is cheap now and load-bearing
for commission later.

### 5.4 Real-Time Inbox

Agents' inboxes update live — new message, assignment change, delivery tick —
through the new `core.realtime.publish_update` (§6.2). **MVP transport
(single EC2, §12): Server-Sent Events.** The worker publishes to Redis
pub/sub (`rt:{channel}`); the API process relays to browsers over SSE
(`GET /omnichannel/stream`) — zero new dependencies, no AppSync bill.
AppSync GraphQL subscriptions become the transport when the service is
distributed; callers never change because everything goes through the
facade. Auth re-checks org membership on connect: a revoked membership must
terminate the stream on the next reconnect. Idle tabs (>5 min in
background) close the stream and reconnect on focus.

### 5.5 Commission Attribution — the load-bearing business rule

**Deferred (§15):** not built until Invoicing (Phase 2) exists —
`invoice.paid` has no producer (§6.0). The *tables* still ship in the v1
schema baseline (they cost nothing, and the append-only assignment history
§5.3 is already being written), so when Invoicing lands this becomes
subscriber code only. The rule below is locked now so nobody "simplifies"
it in the meantime:

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

**Inbound:** generic webhook route `POST /webhooks/{channel_type}/{connection_id}`
— one route for every channel; each org's channel connection registers its
own URL, so `connection_id` resolves the org and its signing secret (via
`core.secrets`) before anything else, and the adapter registry supplies
signature verification — a new channel adds no routing code: resolve
connection → verify signature → ack fast (<2s p99, just validate + enqueue
to the **shared inbound SQS queue** with `channel_type` / `org_id` /
`connection_id` message attributes — one queue for all channels; split a
channel out only if its volume ever demands isolation) ⇒ worker: dedupe on
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
retry with backoff, then DLQ + alarm — never blind infinite retry (WhatsApp
sends cost real money per attempt).

---

# PART II — HOW IT WIRES INTO A2Z-CORE (⚠ ADAPTED)

## 6. Reality Check & Core Dependency Map

### 6.0 What exists in this repo today (⚠ ADAPTED)

The original plan assumed Invoicing (Phase 2) was already built. It is not:
`app/services/invoicing/` is an empty stub; only `docs/phase2-invoicing.md`
exists. Consequences:

1. **The shared Postgres foundation does not exist yet** — no Postgres
   container anywhere, no SQLAlchemy/asyncpg/alembic in `pyproject.toml`.
   Whichever phase starts first builds it (the container in §12, per
   `docs/phase2-invoicing.md` step 1); the other reuses it (shared
   instance, separate schema).
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
| `core.clients` | The **only** place AWS/Redis clients are built. New clients (Secrets Manager now; AppSync at distribution) are added there as `@lru_cache` factories; every sync boto3 call goes through `await core.clients.run_aws(fn, ...)`. |

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
    """Push a real-time update to connected clients.

    - channel examples: f"org:{org_id}:conversations",
      f"conversation:{conversation_id}:messages"
    - The transport lives entirely behind this call. MVP (single EC2, §12):
      Redis pub/sub (``rt:{channel}``, shared client) relayed to browsers as
      SSE by the API process. Distribution phase: an AppSync GraphQL mutation
      over httpx with IAM SigV4 signing via botocore's SigV4Auth — no new
      signing library. Callers never change when the transport swaps.
    - Fire-and-forget from caller's perspective; errors logged, not swallowed.

    Raises: RealtimeError (CoreError, 502) only on config errors.
    Performance: < 100ms.
    """
```

> **Test-harness note (⚠ ADAPTED):** Core's integration tests run on **moto +
> fakeredis**, not LocalStack. moto covers Secrets Manager (add the extra).
> The MVP Redis-pub/sub transport tests directly against fakeredis. When the
> AppSync transport lands (distribution phase), moto has no usable AppSync
> data plane — unit-test it against a stubbed transport (httpx
> `MockTransport`) and flag real-AWS verification explicitly in the test
> plan; don't skip it silently.

### 6.3 Cross-service rule (non-negotiable)

Omni-Channel and Invoicing never import each other's code. Events only:
Omni-Channel publishes `conversation.invoice_requested` → Invoicing consumes;
Invoicing publishes `invoice.paid` → Omni-Channel consumes (§5.5).

**⚠ ADAPTED:** Core owns only the *publisher*. Subscribers are service-owned:
an EventBridge **rule** on `a2z-bus` (`source = a2z.invoicing`,
`detail-type = invoice.paid`) targets this service's **events SQS queue**;
the worker process (§12) consumes it. Rule + queue live in this service's
Terragrunt modules (§12).

### 6.4 Rate-limit registry additions (`app/config.py`)

```python
RATE_LIMITS: dict[str, tuple[int, int]] = {
    # ... existing entries ...
    "omnichannel.whatsapp.send": (80, 1),      # Meta pair-rate ceiling; tune per tier
    # omnichannel.sms.send: add when SMS is un-deferred (§15) — provider throughput cap per org
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
| Redis | `redis.asyncio` via `core.clients.redis_client()` — shared client, namespaced keys (`presence:{org_id}:*`, `secret:*`, `mediaurl:{key}`, `rt:*`). |
| DTOs / validation | `pydantic` v2 — adapter types, models, everything. |
| Config | `pydantic-settings` — new fields on `app/config.py::Settings` with env aliases (`DATABASE_URL`, SQS queue URLs; `APPSYNC_ENDPOINT` at distribution). No per-service config module. |
| JWT | `python-jose`, already wrapped by `core.auth`; never used directly here. |
| Webhook signatures | stdlib `hmac` + `hashlib`. No new dependency. |
| Inbound MIME parsing | stdlib `email` package. No new dependency. |
| Lint / types | `ruff` (line-length 100, E,F,I,B,UP,ASYNC) + `mypy --strict` with pydantic plugin — config already in `pyproject.toml`, no per-service overrides. |
| Tests | `pytest` + `pytest-asyncio` (auto mode), `moto`, `fakeredis`, `httpx` — same harness as Core. **Not LocalStack** (docker-compose LocalStack is manual-dev only; CI uses moto). |

**New dependencies this service may add:**

| Dependency | Why | Where |
|---|---|---|
| `sqlalchemy[asyncio]`, `asyncpg`, `alembic` | Postgres data layer — the same deps `docs/phase2-invoicing.md` step 1 earmarks for Invoicing. Add once; both services share. | `[project.dependencies]` |
| `httpx` | Runtime HTTP for WhatsApp Graph API (and AppSync mutations at distribution; SMS provider when un-deferred). Already a dev dep — promote. | move to `[project.dependencies]` |
| `moto[secretsmanager]`, `boto3-stubs[secretsmanager,sqs]` extras | Test/typing for the new AWS surfaces. | dev extras |

**Explicitly NOT added (⚠ ADAPTED deviations):**

- `aws-secretsmanager-caching` — replaced by the Redis idiom (§6.2).
- **Sentry** — the original plan kept it alongside CloudWatch; that violates
  the AWS-only principle and adds a dep Core doesn't have. CloudWatch Logs
  Insights + alarms + X-Ray cover it. Revisit deliberately only if exception
  grouping proves genuinely missing.
- Any WebSocket library — MVP real-time is SSE (a plain FastAPI streaming
  response, zero new deps) fed by Redis pub/sub behind `core.realtime`;
  AppSync takes over at distribution. Nothing ever imports a socket library.
- Bedrock or any AI client — AI features are cut from v1 entirely (§15).

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
│   │   ├── adapters/                  # base.py, types.py, whatsapp.py, email.py, registry.py (sms.py when un-deferred, §15)
│   │   ├── routing.py                 # §5.3 strategies
│   │   ├── presence.py                # Redis-backed presence
│   │   ├── commission.py              # §5.5 attribution + invoice.paid handler
│   │   ├── handlers.py                # business logic called by routers
│   │   ├── webhooks/                  # generic dispatch: verify via adapter registry, enqueue (§5.6)
│   │   └── worker.py                  # worker process: SQS consumer (§5.6)
│   ├── routers/omnichannel.py         # thin HTTP layer incl. POST /webhooks/{channel_type}/{connection_id},
│   │                                  # mounted in app/main.py — no per-channel Lambdas
├── infra/
│   ├── modules/                       # NEW (§12): ec2-app, sqs-omnichannel,
│   │   ...                            #      secretsmanager-channels, ses-receipt-rules
│   └── live/prod/<name>/terragrunt.hcl   # one live folder per module — existing layout
└── tests/{unit,integration,load}/omnichannel/
```

**Reused, not re-provisioned:** the box's Postgres container (new
`omnichannel` schema — Invoicing adds its own schema to the same container
later), the Redis container (new namespaces), the SES sending pipeline (via
`core.email`), Core's VPC/IAM modules. Alembic migrations live with the
service; DynamoDB backfill scripts stay in `infra/migrations/` per root
CLAUDE.md §9.

## 10. Cost & Caching (all via the shared Redis client)

Every cache uses `core.clients.redis_client()` with a namespaced key — no
second cache system:

| Cost vector | Mitigation |
|---|---|
| SES volume | batch notifications ("3 new messages"), org's own verified domain (Core config sets already per org/service) |
| Real-time fan-out | SSE on-box = $0 at MVP; idle tabs close streams after 5 min (matters again when AppSync lands) |
| Postgres bloat | messages >12 months to cold partition; hot inbox queries never touch archive |
| S3 egress | `mediaurl:{key}` signed-URL cache, 1h TTL (< signed expiry) |
| NAT Gateway | none at MVP — the EC2 sits in a public subnet with its own IPv4; at distribution, add VPC endpoints for S3/SQS/Secrets Manager/SES *before* adding a NAT (the silent-killer line item) |
| Secrets Manager API | 5-min Redis TTL in `core.secrets` (§6.2) |

**MVP fixed spend (single EC2, §12):**

| Item | Est. monthly |
|---|---|
| EC2 t4g.medium (4 GB — API + worker + Postgres + Redis) | ~$25 |
| EBS gp3 50 GB + snapshots | ~$5 |
| Public IPv4 | ~$4 |
| Secrets Manager | ~$0.40 per channel credential |
| DynamoDB / S3 / SES / EventBridge / SQS / CloudWatch | usage-based, ~$1–10 |

≈ **$35–45/mo all-in**, vs ≈$130/mo for the distributed shape (Fargate +
RDS + ElastiCache + NAT + ALB) — same code, different Terragrunt. SES
usage passes through to customers (WhatsApp is free-tier for
customer-initiated conversations; SMS pass-through applies once un-deferred).

## 11. Observability (CloudWatch only — ⚠ ADAPTED: no Sentry)

- Structured JSON logs via `core.logging` with `request_id` /
  `conversation_id` / `message_id` threaded through every line of a flow —
  the primary trace at MVP (one box, one log group via the CloudWatch
  agent). Add X-Ray when the service is distributed, not before.
- Namespace `A2Z/OmniChannel`: `WebhookAckLatencyMs` per channel (alarm p99
  > 2s — Meta's retry window is ~10s), `MessageProcessingLatencyMs` (receipt
  → visible in inbox), `RoutingLatencyMs`, `SendSuccessRate`/`SendFailureRate`
  per channel, `ActiveSSEStreams` (becomes `ActiveAppSyncConnections` at
  distribution).
- Alarms: any DLQ depth > 0, webhook ack p99 breach, send failure rate > 5%,
  EC2 status-check failure, disk > 80% (Postgres lives on this box), nightly
  backup job failure (§12).

## 12. Deployment & Terragrunt — single-EC2 MVP (⚠ ADAPTED 2026-07-12)

MVP runs the whole platform on **one EC2 instance** (t4g.medium, 4 GB —
sized for four co-located workloads; public subnet; docker-compose or
systemd units):

- **api** — the FastAPI monolith (Core + this service's routers, including
  the generic webhook route §5.6 and the SSE stream §5.4). TLS terminates
  on-box via Caddy (Let's Encrypt) — webhook providers require valid HTTPS;
  no ALB.
- **worker** — the SQS consumer process (§5.6). Same image, different
  entrypoint.
- **postgres** — container, `omnichannel` schema (Invoicing adds its own
  schema to this same container later). **Nightly `pg_dump` to S3, 30-day
  retention, restore-tested — non-negotiable: there is no RDS safety net.**
- **redis** — container, cache/ephemeral semantics only (presence,
  rate-limit windows, caches, pub/sub — all safe to lose on restart); no
  persistence config.

Managed AWS services stay managed — they are usage-based (~$0 at MVP) *and*
they are the seams that make later distribution cheap: DynamoDB, S3, SES,
EventBridge, **SQS (keep it — the webhook-ack/worker seam; an in-process
queue would save nothing and cost the distribution path)**, **Secrets
Manager (keep it — no credential migration at distribution time)**.

**Deliberately deferred to the distribution phase (do not build now):**
ECS/Fargate, RDS, ElastiCache, ALB, NAT + interface VPC endpoints, AppSync,
per-channel webhook Lambdas. Distributing later = re-point Terragrunt, move
Postgres/Redis to managed instances, and swap the `core.realtime` transport
— application code does not change.

**Known MVP trade-offs (accepted):** single point of failure — a reboot
takes down webhook endpoints (providers retry with backoff, so brief deploys
are fine; extended downtime loses messages), and Postgres durability is our
job (hence the backup rule above).

New `infra/modules/`: `ec2-app/` (instance, EIP, security group, IAM
instance profile reusing the `iam` module's policies), `sqs-omnichannel/`
(**one shared inbound queue** §5.6 + outbound + events queues, DLQs on all,
the EventBridge rule targeting the events queue §6.3, and the S3 event
notification for inbound email §5.2), `secretsmanager-channels/`,
`ses-receipt-rules/`. Each gets a matching
`infra/live/prod/<name>/terragrunt.hcl` like the nine existing ones. Reused
unmodified: `vpc` (gateway endpoints only), `iam`, `s3`, `ses`,
`eventbridge`, `dynamodb`. **Not applied at MVP:** the `redis` and `ecs`
live modules (superseded by the on-box containers until distribution).
Mirror what tests need in `scripts/create_local_resources.py`.

## 13. Build Order (⚠ ADAPTED for actual repo state)

**Step 0 — Prerequisite check:** Core suite green locally. Decide Phase 2/3
ordering: if Invoicing hasn't built the shared Postgres foundation (the
container in §12 + the SQLAlchemy/alembic deps), Step 2 here includes it
(Invoicing reuses it later).

**Step 1 — Core unfreeze (one deliberate change):** `core/secrets.py` +
`core/realtime.py` + client factories in `core/clients.py` + `Settings`
fields + `RATE_LIMITS` entries + root CLAUDE.md module-table rows + any new
`ActionType`/error classes. Full Core suite + coverage bar green. Re-freeze.

**Step 2 — Data layer:** deps (`sqlalchemy[asyncio]`, `asyncpg`, `alembic`,
promote `httpx`), the shared Postgres container (§12), `omnichannel` schema,
Alembic baseline with all tables/indexes/the unique constraint (§5.1) —
`channel_type` as `TEXT`. Postgres added to `docker-compose.yml` for
local/integration tests. Record §14 decisions in
`docs/omnichannel-decisions.md` first.

**Step 3 — Adapter contract + Email adapter first** — reuses `core.email`
almost entirely, so it validates the adapter pattern with the least new
surface area.

**Step 4 — WhatsApp adapter:** registry entry (webhooks arrive via the
generic route, §5.6), normalize, send, delivery interpretation, credentials
via `core.secrets`. (SMS adapter deferred, §15 — add the same way when
un-deferred.)

**Step 5 — End-to-end message flow (§5.6):** webhook route → SQS → worker →
persistence → `core.events` fan-out; webhook-retry/duplicate tests.

**Step 6 — Assignment (§5.3, v1 scope):** manual claim/reassign +
single-assignee, append-only `conversation_assignments` rows + audit. No
presence, no auto-routing (deferred, §15).
**Step 7 — Real-time inbox (§5.4)** via `core.realtime` (SSE + Redis
pub/sub), idle-tab backpressure.
**Step 8 — Load + integration tests,** latency targets from §11, DLQ/alarm
wiring, then freeze.

Cut/deferred from the original order (§15): commission (waits for Invoicing;
its tables are already in the Step 2 baseline), auto-routing + presence,
templates, AI features (cut entirely).

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
6. Pricing tier shape ($49–79/mo + WhatsApp pass-through, SMS pass-through
   once un-deferred) — Settings/Billing need the shape, not this service's
   code.

## 15. Out of Scope (don't add now)

**Cut/deferred in the 2026-07-12 minimal-scope revision:**

- **AI features** (Bedrock summaries, suggested replies, classification
  cache, its rate limit) — cut entirely; re-propose as a separate phase if
  ever wanted.
- **Auto-routing (round-robin, sticky) + presence** — v1 is manual claim +
  single-assignee (§5.3). The append-only assignment history is still
  written from day one, so adding auto-routing later is routing code only.
- **Templates** — deferred. Accepted consequence: without WhatsApp-approved
  templates, agents can only reply inside WhatsApp's 24-hour
  customer-service window; business-initiated WhatsApp messages are not
  possible in v1. Email is unaffected.
- **SMS channel** — deferred. v1 ships WhatsApp + email only. The adapter
  contract (§5.2, §7) makes this add-later: one new adapter file + a
  registry entry, no changes to routing, storage, or infra.
- **Commission attribution (§5.5)** — waits for Invoicing (Phase 2); its
  tables ship in the v1 schema so it's subscriber code only when it lands.
- **Dashboards/analytics** — v1 has ops metrics (§11) only.

**Later versions / never:** Instagram DM / Messenger (v1.1 — two more
adapter files, nothing else changes), voice/Amazon Connect (v1.5),
skill-based routing & SLA escalation (v2), split commissions (v1.5 — schema
already allows multiple attributions per invoice), public Inbox API
(post-launch), a Permissions service (never — inline role checks).

## 16. Definition of Done

- [x] `core/secrets.py` + `core/realtime.py` built to Core's bar, module
      table updated, full Core suite green (no regressions), Core re-frozen.
      *(Done 2026-07-12 — Build Order Step 1. 81 tests, 93% core coverage,
      `ruff` + `mypy --strict` clean. `realtime.publish_update` ships as
      Redis pub/sub only for now, matching the single-EC2 MVP transport in
      §5.4/§12 — no AppSync client exists yet; that's added when the
      service distributes.)*
- [x] `sqlalchemy[asyncio]`/`asyncpg`/`alembic` added once; `httpx` promoted;
      no non-conforming deps (no Sentry, no aioboto3, no
      aws-secretsmanager-caching). *(Done 2026-07-13 — Build Order Step 2.)*
- [x] `omnichannel` Postgres schema with all §5.1 tables + indexes + the
      `(channel_type, external_message_id)` unique constraint.
      *(`app/services/omnichannel/models.py` + Alembic baseline
      `migrations/versions/0001_baseline_schema.py`; `channel_type` is
      `TEXT` everywhere per the extensibility invariant, guarded by a test.
      Upgrade/downgrade/re-upgrade verified against a real local Postgres 16
      — column-by-column, including the hand-added full-text GIN index that
      autogenerate can't produce. §14 decisions recorded first in
      `docs/omnichannel-decisions.md`. `docker-compose.yml` and CI
      (`.github/workflows/ci.yml`) both gained a `postgres` service — the
      one exception to Core's moto/fakeredis-only test posture, since
      there's no in-process Postgres emulator. 5 new integration tests
      under `tests/integration/omnichannel/` cover the idempotency unique
      constraint, the identity unique constraint, FK enforcement, cross-org
      query isolation, and the `channel_type` TEXT invariant.)*
- [x] `ChannelAdapter` Protocol (`adapters/base.py`) + shared types
      (`adapters/types.py`) + registry (`adapters/registry.py`) built;
      swappable and testable in isolation. *(Done 2026-07-13 — Build Order
      Step 3.)*
- [x] Email adapter (`adapters/email.py`) implements `ChannelAdapter`; sends
      go through `core.email.send_email` only, never boto3 SES directly.
      `verify_inbound_signature` is a documented no-op (inbound email has no
      HTTP webhook to sign — it arrives via SES receipt rule → S3 → SQS,
      §5.2); `normalize_inbound` parses raw MIME with stdlib `email`, no new
      dependency. *(Done 2026-07-13 — Build Order Step 3. 12 new unit tests
      under `tests/unit/omnichannel/`, `ruff` + `mypy --strict` clean, full
      suite green — 98 tests, no regressions.)*
- [x] WhatsApp adapter (`adapters/whatsapp.py`) implements `ChannelAdapter`;
      registry entry added (SMS still deferred, §15); credentials
      (`org_id`/`access_token`/`phone_number_id`) arrive via `credentials`,
      resolved by the caller through `core.secrets` — the adapter itself
      never calls `core.secrets`. `verify_inbound_signature` checks Meta's
      `X-Hub-Signature-256` HMAC-SHA256 over the raw body.
      `omnichannel.whatsapp.send` (80/1s) added to `app/config.py::RATE_LIMITS`
      per §6.4. Two v1 scope decisions made explicit rather than silent:
      **outbound is text-only** (no template support yet, and WhatsApp
      requires an approved template to business-initiate outside the 24h
      window — templates are deferred, §15); **inbound media is recorded
      without its bytes** (non-text messages carry only a Graph API media id,
      and downloading it needs a second credentialed call that
      `normalize_inbound`'s Protocol signature has no room for — the message
      still persists with a placeholder body so idempotency and customer
      visibility hold). *(Done 2026-07-13 — Build Order Step 4. 14 new unit
      tests under `tests/unit/omnichannel/test_whatsapp_adapter.py`
      (signature verification incl. case-insensitive headers, multi-message
      normalization, the media gap, outbound success/validation/HTTP-error
      wrapping, status-webhook mapping) plus a 2-test registry update;
      `ruff` + `mypy --strict` clean; full suite green — 112 tests, no
      regressions.)*
- [ ] Inbound/outbound flows match §5.6; webhook-retry/duplicate tests pass;
      all rate limits read from `app/config.py::RATE_LIMITS`.
- [ ] v1 assignment working (manual claim/reassign + single-assignee) with
      append-only assignment history; SSE real-time via `core.realtime`
      (Redis pub/sub transport) with idle-tab backpressure.
- [ ] Extensibility invariants hold (§5.2): `channel_type` is `TEXT`, one
      generic webhook route, one shared inbound queue — adding a channel
      touches only `adapters/` + the registry.
- [ ] Commission tables present in the baseline schema (feature itself
      deferred with Invoicing, §15 — the invoice-creation snapshot rule in
      §5.5 stays locked for when it's built).
- [ ] All §10 cost mitigations implemented, not deferred.
- [ ] CloudWatch logs / X-Ray / `A2Z/OmniChannel` metrics / alarms live.
- [ ] Terragrunt modules applied per §12 (single-EC2 shape; deferred modules
      not built); local resources mirrored in
      `scripts/create_local_resources.py`.
- [ ] Nightly Postgres `pg_dump` to S3 running, 30-day retention, and a
      restore actually tested (§12).
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
