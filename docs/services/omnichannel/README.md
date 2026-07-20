# Omni-Channel Service

> Part of the [documentation index](../../README.md). See also: [architecture overview](../../architecture/overview.md), [`app/services/omnichannel/CLAUDE.md`](../../../app/services/omnichannel/CLAUDE.md) (the original build plan — treat this `docs/` tree as the current-state reference; the `CLAUDE.md` as the historical design record).
> **Authority:** _reference_ — describes current code; if the two disagree, the code wins.

## Purpose & responsibilities

A multi-tenant **unified inbox**: every customer message, from every
connected channel (WhatsApp, email; SMS built but not wired in — see
[known limitations](known-issues.md)), lands in one conversation view per
org. Agents claim or get assigned conversations and reply from one screen;
the reply goes back out through whichever channel the customer used. This
is the first product service built on Core, and the proof that Core's
platform layer generalizes beyond its own design.

## High-level overview

```mermaid
flowchart LR
    Customer["Customer\n(WhatsApp / email)"] <-->|messages| Channel["Channel provider\n(Meta Graph API / SES)"]
    Channel <-->|"webhook in / API call out"| OC["Omni-Channel service"]
    OC <--> Agent["Agent (browser, SSE-connected)"]
    OC --> Core["A2Z Core\n(auth, storage, secrets, events, realtime, rate_limit)"]
```

## Documents in this section

| Doc | Covers |
|---|---|
| [`data-model.md`](data-model.md) | Postgres schema, ERD, migrations, SQS provisioning |
| [`adapters.md`](adapters.md) | The `ChannelAdapter` contract; email, WhatsApp, SMS adapters |
| [`message-flow.md`](message-flow.md) | Inbound/outbound pipeline, webhook handling, the worker, idempotency |
| [`routing-and-realtime.md`](routing-and-realtime.md) | Claim/reassign/single-assignee, presence, SSE realtime inbox |
| [`api-reference.md`](api-reference.md) | Every HTTP endpoint this service mounts |
| [`known-issues.md`](known-issues.md) | Documented drift between the design doc and the actual implementation, and open gaps |

## Internal architecture at a glance

```mermaid
flowchart TB
    subgraph API["API process (app/routers/omnichannel.py)"]
        Webhook["POST /webhooks/{channel_type}/{connection_id}"]
        Inbox["GET conversations / conversation / read"]
        Reply["POST .../messages (send_reply)"]
        Assign["POST .../claim, .../reassign, PUT .../routing-config"]
        Stream["GET .../stream (SSE)"]
    end
    subgraph Business["Business logic (app/services/omnichannel/*.py)"]
        Webhooks_py["webhooks.py\n(verify + enqueue)"]
        Handlers["handlers.py\n(send_reply)"]
        Inbox_py["inbox.py\n(reads)"]
        Routing["routing.py\n(claim/reassign/single-assignee)"]
        Presence["presence.py"]
        Stream_py["stream.py\n(SSE relay)"]
    end
    subgraph WorkerProc["Worker process (worker.py)"]
        InboundProc["process_inbound_batch"]
        OutboundProc["process_outbound_batch"]
    end
    subgraph Adapters["adapters/ (registry.py dispatches)"]
        EmailA["email.py"]
        WhatsAppA["whatsapp.py"]
        SmsA["sms.py (built, not registered)"]
    end
    Webhook --> Webhooks_py --> SQSIn[("SQS inbound")]
    SQSIn --> InboundProc --> Adapters
    Reply --> Handlers --> SQSOut[("SQS outbound")]
    SQSOut --> OutboundProc --> Adapters
    Inbox --> Inbox_py
    Assign --> Routing
    Stream --> Stream_py
    Routing --> Presence
```

## Public interfaces

- **HTTP** — mounted at `/v1/omnichannel/*` in `app/main.py` (versioning
  policy: [root API reference](../../api-reference.md#versioning)); full
  reference in [`api-reference.md`](api-reference.md).
- **Events** — publishes `message.received`, `message.sent`,
  `conversation.assigned` on `a2z-bus` (`source="a2z.omnichannel"`);
  designed-but-unpublished `conversation.invoice_requested`; consumes (once
  Invoicing exists) `invoice.paid`. See
  [event-driven architecture](../../architecture/event-driven-architecture.md)
  and [`docs/events.md`](../../events.md).
- **Worker process** — same container image, different entrypoint
  (`worker.py`'s `process_inbound_batch`/`process_outbound_batch` run in a
  loop); see [message flow](message-flow.md).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://a2z:a2z-local-dev-only@localhost:5432/a2z` | Shared Postgres instance, `omnichannel` schema |
| `OMNICHANNEL_INBOUND_QUEUE` / `_DLQ` | `a2z-omnichannel-inbound[-dlq]` | SQS queue names |
| `OMNICHANNEL_OUTBOUND_QUEUE` / `_DLQ` | `a2z-omnichannel-outbound[-dlq]` | SQS queue names |

`RATE_LIMITS["omnichannel.whatsapp.send"] = (80, 1)` (`app/config.py`) is the
one channel-specific outbound rate limit registered so far — email needs
none (`core.email` already enforces its own).

## Dependencies

- **On Core**: `auth`, `membership`, `audit`, `settings` (via the
  `metadata["omnichannel"]` escape hatch), `events`, `rate_limit`,
  `storage`, `email`, `secrets`, `realtime`, `clients`, `logging`,
  `exceptions`. See [Core module reference](../../core/README.md).
- **External**: SQLAlchemy (async) + asyncpg + Alembic for Postgres; `httpx`
  for the WhatsApp Graph API; stdlib `email`/`hmac`/`hashlib` for MIME
  parsing and webhook signature verification.
- **Never**: imports from `app/services/invoicing/` or any other service —
  cross-service communication is events only (golden rule #4).

## Data model

See [`data-model.md`](data-model.md) for the full ERD, table-by-table
description, index rationale, and the migration history (including a
documented inconsistency in the migration chain).

## Error handling

`app/services/omnichannel/exceptions.py` extends `core.exceptions.CoreError`
with service-specific subclasses (`ChannelAdapterError` 502,
`WebhookSignatureError` 401, `ConnectionNotFoundError` 404, `RoutingError`
400, `CommissionError` 409, `ConversationNotFoundError` 404, `ForbiddenError`
403, `ConversationAlreadyAssignedError` 409, `InvalidQueryError` 400,
`ConnectionValidationError` 400) — mapped to HTTP responses by
the same global handler as every Core error (see
[request lifecycle](../../architecture/request-lifecycle.md)).

## Security considerations

- Every table carries `org_id`; every query filters on it — see
  [data flow](../../architecture/data-flow.md#the-org-scoping-invariant).
- Inbound webhooks are signature-verified before anything else runs (HMAC
  for WhatsApp; a documented no-op for email, which never receives an HTTP
  webhook in the first place).
- The role model gap between this service's product roles
  (Owner/Admin/Agent/Viewer) and Core's `Role` enum
  (OWNER/ADMIN/MEMBER/GUEST) is explicit and consistently mapped — see
  [auth & authorization](../../architecture/auth-and-authorization.md#role-mapping-gap-documented-not-silently-resolved).

## Example usage

See [`api-reference.md`](api-reference.md) for full request/response shapes.
A minimal flow: `POST /v1/omnichannel/orgs/{org_id}/connections` registers a
channel → customer messages it → webhook lands → worker creates the
conversation → agent sees it via
`GET /v1/omnichannel/orgs/{org_id}/conversations` → agent replies via
`POST .../messages` → worker sends it out.

## Common extension points

Adding a new channel is, by design, the smallest change this service
supports: one new file in `adapters/` implementing `ChannelAdapter`, one
line in `adapters/registry.py`. See [`adapters.md`](adapters.md) for the
three invariants that keep this true (`channel_type` as `TEXT`, one generic
webhook route, one shared SQS queue pair).

## Known limitations

See [`known-issues.md`](known-issues.md) for the full list — notably: SMS
has a working adapter that isn't registered; presence/round-robin routing
described as "deferred" in the design doc is partly implemented; commission
attribution and AI features are genuinely not built; and there's an
orphaned duplicate migration file that would make `alembic upgrade head`
ambiguous against a fresh database.
