# Omni-Channel API Reference

> Part of the [Omni-Channel service docs](README.md). Source: [`app/routers/omnichannel.py`](../../../app/routers/omnichannel.py). All routes are mounted under `/v1/omnichannel` (see [root API reference: Versioning](../../api-reference.md#versioning)) and require `Authorization: Bearer <jwt>` unless noted; errors follow the [typed `CoreError` convention](../../architecture/request-lifecycle.md#error-handling--one-exception-hierarchy-one-handler).
> **Authority:** _reference_ — describes current code; if the two disagree, the code wins.

## Webhooks

### `POST /v1/omnichannel/webhooks/{channel_type}/{connection_id}`

Generic inbound webhook route — one route for every channel. **No bearer
auth**: authenticity is established by the channel's own signature
(verified via the adapter registry), not a JWT.

- **Path params**: `channel_type` (`"email"` | `"whatsapp"`), `connection_id`
  (the `channel_connections.id` this webhook is registered against).
- **Body**: raw, channel-specific payload (JSON for WhatsApp; not used for
  email, which never hits this route — see [message flow](message-flow.md)).
- **Response**: `{"status": "accepted"}` on success.
- **Errors**: `404 ConnectionNotFoundError` (unknown connection, disabled
  connection, or `channel_type` mismatch), `401 WebhookSignatureError`.
- **Performance target**: < 2s p99 (ack fast, real work happens in the
  worker — Meta's retry window is ~10s).

### `GET /v1/omnichannel/webhooks/{channel_type}/{connection_id}`

Answers a provider's webhook-subscription verification handshake (API
review, 2026-07-18) — Meta's WhatsApp Cloud API calls this once, with
`?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`, when the
webhook URL is registered in the Meta App dashboard, and will not deliver
real events to a URL that hasn't answered it correctly. **No bearer auth**
(same reasoning as the `POST` route): authenticity is the channel's own
`verify_token` check, compared against the `verify_token` field in the
connection's `core.secrets` bundle.

- **Response**: the raw `hub.challenge` value, as `text/plain` — **not**
  a JSON envelope; providers expect the literal challenge string back.
- **Errors**: `404 ConnectionNotFoundError` (unknown/disabled connection,
  channel_type mismatch), `502 ChannelAdapterError` (wrong/missing
  verify token, or the channel has no such handshake — email and SMS
  both reject any call here outright, since neither has a public webhook
  to verify).

## Connections

### `POST /v1/omnichannel/orgs/{org_id}/connections`

Register a channel connection (Owner/Admin only, API review 2026-07-18 —
previously there was no API path to create one at all). Body:
`{"channel_type": str, "display_name": str, "provider_account_id": str,
"credentials_secret_key": str}`. `credentials_secret_key` is a *reference*
into `core.secrets` (`a2z/{org_id}/omnichannel/{key}`) — never a raw
credential; the secret itself must already exist (provisioned out of band).
Returns `201` + a `ConnectionView` (never includes the resolved secret
value, only the reference key). `400 ConnectionValidationError` if
`channel_type` has no registered adapter (e.g. `"sms"` — a full adapter
exists but is deliberately unregistered, see
[known limitations](known-issues.md)). `404 SecretNotFoundError` if the
secret key doesn't resolve.

### `GET /v1/omnichannel/orgs/{org_id}/connections`

List an org's connections (Owner/Admin only). Returns `{"items":
[ConnectionView, ...]}`.

### `GET /v1/omnichannel/orgs/{org_id}/connections/{connection_id}`

Read one connection (Owner/Admin only). `404 ConnectionNotFoundError` if
missing or cross-org.

### `DELETE /v1/omnichannel/orgs/{org_id}/connections/{connection_id}`

Soft-disables a connection (Owner/Admin only) — sets `status="disabled"`
rather than deleting the row. Returns `204`. From this point on, the
connection's inbound webhooks (both `POST` and the `GET` handshake) are
rejected with `404 ConnectionNotFoundError`, as if the connection didn't
exist.

```python
class ConnectionView(BaseModel):
    id: str; channel_type: str; display_name: str
    provider_account_id: str; credentials_secret_key: str  # a reference, not a secret
    status: str; created_at: datetime; updated_at: datetime
```

## Inbox reads

### `GET /v1/omnichannel/orgs/{org_id}/conversations`

List an org's conversations. Requires membership only (any role — see
[role mapping](../../architecture/auth-and-authorization.md#role-mapping-gap-documented-not-silently-resolved)).

| Query param | Default | Meaning |
|---|---|---|
| `status` | — | Filter `open`/`pending`/`closed` |
| `assigned_user_id` | — | Filter to one agent's inbox |
| `q` | — | Search: matches customer display name (`ILIKE`) or any message body in the thread (Postgres full-text) |
| `sort` | `-last_message_at` | `-last_message_at` (newest first) or `last_message_at` (oldest first) |
| `limit` | 50 | Clamped to 100 |
| `cursor` | — | Opaque continuation token from a previous page's `next_cursor` |

Returns `{"items": [ConversationSummary, ...], "next_cursor": str | null}`.
**Cursor pagination, not offset** (API review, 2026-07-18): `next_cursor`
encodes `(last_message_at, id)`, the exact tuple the query sorts on, so a
page is stable under concurrent inserts — a new inbound message can't shift
or duplicate rows across pages the way `offset` would. `null` means this
was the last page. There is no way to jump to an arbitrary page or get a
total count by design (a total would cost a separate `COUNT` query for no
real product need).

### `GET /v1/omnichannel/orgs/{org_id}/conversations/{conversation_id}`

One conversation's thread — the most recent `limit` (default 50, max 100)
messages, oldest-first, plus signed attachment URLs. `before` (a previous
response's `messages_next_cursor`) fetches the next-older page, for
scrolling back through a long thread. Returns `ConversationDetail`
(`messages_next_cursor: str | null` — set when older messages exist).
`404 ConversationNotFoundError` if the conversation doesn't exist *for this
org* (deliberately the same error whether it's missing or belongs to
another org).

### `POST /v1/omnichannel/orgs/{org_id}/conversations/{conversation_id}/read`

Zeroes the conversation's unread counter. A separate POST rather than a
side effect of the GET above — a prefetch or double-render must not
silently clear an agent's unread badge. Returns
`{"conversation_id": str, "unread_count": 0}`.

## Sending & assignment

### `POST /v1/omnichannel/orgs/{org_id}/conversations/{conversation_id}/messages`

Send an agent's reply. Body: `{"body_text": str}`. Requires membership and
role ≠ GUEST/Viewer. Persists as `"queued"` and enqueues for the worker —
does **not** wait for the actual channel send. Returns `201` +
`{"message_id": str, "status": "queued"}`.

**Idempotency** (API review, 2026-07-18): an optional `Idempotency-Key`
header. Retrying the same request with the same key returns the *original*
message instead of sending a duplicate, and the response status drops to
`200` (a replay, not a new creation — `201` is reserved for an actual new
send). The key is scoped per `(org_id, conversation_id)`; the same key in
a different conversation is not a collision. Omitting the header preserves
the original always-a-fresh-send behavior.

`429 RateLimitError` if the channel's outbound rate limit is registered and
exceeded (not charged against a replayed request — the dedup check runs
before the rate limiter).

### `POST /v1/omnichannel/orgs/{org_id}/conversations/{conversation_id}/claim`

Agent claims an unassigned conversation. Idempotent if already the caller's.
`409 ConversationAlreadyAssignedError` if owned by someone else (use
reassign instead). Returns `{"conversation_id", "assigned_user_id"}`.

### `POST /v1/omnichannel/orgs/{org_id}/conversations/{conversation_id}/reassign`

Owner/Admin only. Body: `{"assignee_user_id": str}` — must be an org
member. Returns `{"conversation_id", "assigned_user_id"}`.

### `PUT /v1/omnichannel/orgs/{org_id}/routing-config`

Owner/Admin only. Body: `{"strategy": "manual" | "single_assignee",
"single_assignee_user_id": str | None}`. Returns
`{"routing_strategy": str, "single_assignee_user_id": str | None}`.
`400 RoutingError` for an unsupported strategy (round-robin/sticky are not
yet implemented — see [known limitations](known-issues.md)); this is a
`400` because every raise site is a request-validation failure, not an
engine fault — see the docstring on `RoutingError` in `exceptions.py` for
the correction history (it shipped as a `500` until the 2026-07-18 API
review).

## Realtime

### `GET /v1/omnichannel/orgs/{org_id}/stream`

Server-Sent Events stream of this agent's live inbox updates (new message,
assignment change, send confirmation). Auth via `access_token` query param
(an `Authorization` header is accepted as a fallback) since browser
`EventSource` cannot set custom headers. `404 NotFoundError` if not a
member. See [routing & realtime](routing-and-realtime.md#realtime-inbox-sse)
for the full protocol.

## Response models (selected)

```python
class ConversationSummary(BaseModel):
    id: str; status: str; assigned_user_id: str | None
    last_message_at: datetime | None; last_message_preview: str | None
    unread_count: int; channel_type: str
    customer_external_id: str; customer_display_name: str | None

class MessageView(BaseModel):
    id: str; direction: str; channel_type: str; body_text: str | None
    content_type: str; status: str; sent_by_user_id: str | None
    external_message_id: str; created_at: datetime
    attachments: list[AttachmentView]
```

See [`inbox.py`](../../../app/services/omnichannel/inbox.py) for the
complete set.

## Related surfaces

- [`docs/api-reference.md`](../../api-reference.md) — the full HTTP surface
  including Core's own `core_admin`/`health` routers, and the versioning
  policy that puts everything except `/health` under `/v1`.
- [Message flow](message-flow.md) — what happens after a webhook/reply is
  accepted.
