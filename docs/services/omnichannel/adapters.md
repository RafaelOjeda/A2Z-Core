# Channel Adapters

> Part of the [Omni-Channel service docs](README.md). Source: [`app/services/omnichannel/adapters/`](../../../app/services/omnichannel/adapters/).
> **Authority:** _reference_ — describes current code; if the two disagree, the code wins.

## Purpose & responsibilities

Everything channel-specific lives behind one `Protocol`. The rest of the
system (worker, routing, inbox) never knows which channel it's touching.
This is what makes "add a channel" a small, bounded change instead of a
cross-cutting one.

## The `ChannelAdapter` contract

```python
# app/services/omnichannel/adapters/base.py
@runtime_checkable
class ChannelAdapter(Protocol):
    supported_features: SupportedFeatures

    async def verify_inbound_signature(self, raw_body: bytes, headers: dict[str, str], secret: str) -> bool: ...
    async def normalize_inbound(self, raw_payload: dict[str, Any]) -> list[NormalizedInboundMessage]: ...
    async def send_outbound(self, to: str, content: OutboundContent, credentials: dict[str, Any]) -> SendResult: ...
    async def interpret_delivery_webhook(self, raw_payload: dict[str, Any]) -> list[DeliveryStatusUpdate]: ...
```

Shared Pydantic types (`adapters/types.py`): `SupportedFeatures` (templates,
rich_media, typing_indicators, read_receipts — callers branch on
*capability*, not channel identity), `OutboundContent`/`OutboundAttachment`,
`SendResult`, `InboundAttachment`/`NormalizedInboundMessage`,
`DeliveryStatusUpdate`.

## Three invariants that keep adding a channel small

```mermaid
flowchart TD
    Invariant1["channel_type is TEXT in Postgres,\nnever an ENUM"] --> Reason1["A new channel never needs\na schema migration"]
    Invariant2["One generic webhook route:\nPOST /webhooks/{channel_type}/{connection_id}"] --> Reason2["A new channel never needs\na new route or Lambda"]
    Invariant3["One shared inbound + outbound\nSQS queue pair for every channel"] --> Reason3["A new channel never needs\nnew infra to provision"]
```

Violating any one of these would silently turn "add a channel" into a
migration-plus-infra project — which is why each is guarded by a test (see
[data model](data-model.md#indexes) and
[message flow](message-flow.md)).

## Registry

```python
# app/services/omnichannel/adapters/registry.py
_REGISTRY: dict[str, ChannelAdapter] = {
    "email": EmailAdapter(),
    "whatsapp": WhatsAppAdapter(),
}
```

`get_adapter(channel_type)` raises `ChannelAdapterError` (502) for an
unregistered type. **`SmsAdapter` exists and is fully implemented
(`adapters/sms.py`) but is not in this registry** — see
[known limitations](known-issues.md) for why that matters.

## Email adapter (`adapters/email.py`)

Built first because it validates the pattern with the least new surface —
outbound reuses `core.email.send_email` almost entirely.

| Method | Behavior |
|---|---|
| `verify_inbound_signature` | Always `True` — inbound email never arrives via the generic webhook route (it's SES receipt rule → S3 → the shared inbound SQS queue), so there is no HTTP signature to check. This is a documented no-op, not a shortcut. |
| `normalize_inbound` | Parses raw MIME (`raw_payload = {"raw_mime": bytes, "external_message_id": str}`) via stdlib `email`, extracting the text/HTML body and any attachments |
| `send_outbound` | Calls `core.email.send_email(org_id, ServiceType.OMNICHANNEL, ...)` — **never boto3 SES directly**. `credentials` carries only `org_id` (email has no per-org channel secret) |
| `interpret_delivery_webhook` | Maps `{"message_id", "status"}` (built by a future subscriber from Core's `email.bounced`/`email.complained` events) to `DeliveryStatusUpdate`. Not invoked by an actual per-channel webhook today — Core's own SES/SNS Lambda already handles bounces/complaints independently |

`supported_features = SupportedFeatures(rich_media=True)`.

## WhatsApp adapter (`adapters/whatsapp.py`)

Meta WhatsApp Cloud (Graph) API over `httpx`.

| Method | Behavior |
|---|---|
| `verify_inbound_signature` | HMAC-SHA256 of the raw body against the connection's `app_secret`, compared to the `X-Hub-Signature-256` header via `hmac.compare_digest` (timing-safe) |
| `normalize_inbound` | Flattens Meta's nested `entry[].changes[].value.{messages[],contacts[]}` batch shape into one `NormalizedInboundMessage` per message |
| `send_outbound` | Requires `org_id`, `access_token`, `phone_number_id` in `credentials` (caller resolves via `core.secrets.get_secret` first). **Text-only** — raises `ChannelAdapterError` if `content.body_text` is empty |
| `interpret_delivery_webhook` | Maps Meta's `sent`/`delivered`/`read`/`failed` status webhook directly — no remapping needed, unlike email |

`supported_features = SupportedFeatures(templates=False, rich_media=False, typing_indicators=False, read_receipts=True)`.

**Two accepted v1 gaps, deliberate and documented in the module itself:**

1. **Outbound is text-only.** WhatsApp requires an approved template to
   business-initiate a conversation outside the 24-hour customer-service
   window; templates are deferred, so v1 WhatsApp can only reply within
   that window.
2. **Inbound media is recorded, not downloaded.** A non-text message (image,
   document, audio, video, location) carries only a Graph API *media id* in
   the webhook payload — fetching the actual bytes needs a second,
   credentialed Graph API call that `normalize_inbound`'s Protocol
   signature has no room for (it takes no credentials). v1 persists these
   messages with a placeholder body (`"[unsupported message type: {type}]"`)
   so the customer's message still shows up and idempotency still holds,
   rather than silently dropping it.

## SMS adapter (`adapters/sms.py`) — built, not wired in

Outbound via AWS SNS SMS (a provider decision recorded in
[`docs/omnichannel-decisions.md`](../../omnichannel-decisions.md) — AWS SNS
over Twilio, to stay AWS-native with no new non-AWS credential to manage).

| Method | Behavior |
|---|---|
| `verify_inbound_signature` | Always `True` — inbound SMS arrives via an SNS topic subscription (two-way SMS), triggered only by AWS itself, structurally the same trust model as email's, not a public forgeable webhook |
| `normalize_inbound` | Parses AWS's two-way-SMS inbound-message notification shape (`originationNumber`, `inboundMessageId`, `messageBody`) |
| `send_outbound` | `clients.sns().publish` with optional `origination_number`/`sender_id` message attributes; catches `ClientError` and returns a `SendResult(status=FAILED)` rather than raising |
| `interpret_delivery_webhook` | Parses an SNS delivery-status-logging notification (`SUCCESS`/`FAILURE`) |

Unlike email/WhatsApp, this adapter takes an `org_id` constructor argument
(`SmsAdapter(org_id)`) rather than being a stateless singleton — inconsistent
with the registry's `_REGISTRY: dict[str, ChannelAdapter]` shape, which
holds pre-constructed singletons. This is one of several signs the adapter
was written but never finished being wired in — see
[known limitations](known-issues.md).

> **Note on the field-name shapes**: the module's own docstring flags that
> the AWS two-way-SMS/delivery-status-logging JSON field names follow AWS's
> *documented* shapes as of when it was written but have not been verified
> against a live SNS topic — the most likely part of this adapter to need a
> correction once actually tested end to end.

## Configuration

`RATE_LIMITS["omnichannel.whatsapp.send"] = (80, 1)` in `app/config.py` is
the one channel-specific outbound rate limit registered so far (Meta's
pair-rate ceiling). SMS has no registered limit (unregistered — consistent
with it not being wired in). Email needs none — `core.email.send_email`
already enforces its own 50/hour/org limit.

## Security considerations

- **Signature verification happens before anything else** in the generic
  webhook route (`webhooks.py::handle_webhook`) — a bad/missing signature
  raises `WebhookSignatureError` (401) before the payload is even
  deserialized past the raw bytes.
- Credentials are resolved by the **caller** (webhook/worker code) via
  `core.secrets.get_secret`, then passed into `send_outbound`/verification
  — no adapter calls `core.secrets` itself. This keeps the adapter layer
  free of any Core coupling beyond the shared Pydantic types.

## Example usage

```python
from app.services.omnichannel.adapters.registry import get_adapter

adapter = get_adapter("whatsapp")
ok = await adapter.verify_inbound_signature(raw_body, headers, app_secret)
messages = await adapter.normalize_inbound(payload)
```

## Extension points

Add a channel: create `adapters/{channel}.py` implementing `ChannelAdapter`,
add one line to `_REGISTRY` in `adapters/registry.py`. Nothing else in the
system (routing, storage, infra) needs to change — see the three invariants
above.

## Known limitations

See [`known-issues.md`](known-issues.md) for the SMS-registration gap and
the AWS SMS payload-shape caveat.
