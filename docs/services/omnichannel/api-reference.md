# Omni-Channel API Reference

> Part of the [Omni-Channel service docs](README.md). Source: [`app/routers/omnichannel.py`](../../../app/routers/omnichannel.py). All routes are mounted under `/omnichannel` and require `Authorization: Bearer <jwt>` unless noted; errors follow the [typed `CoreError` convention](../../architecture/request-lifecycle.md#error-handling--one-exception-hierarchy-one-handler).

## Webhooks

### `POST /omnichannel/webhooks/{channel_type}/{connection_id}`

Generic inbound webhook route — one route for every channel. **No bearer
auth**: authenticity is established by the channel's own signature
(verified via the adapter registry), not a JWT.

- **Path params**: `channel_type` (`"email"` | `"whatsapp"`), `connection_id`
  (the `channel_connections.id` this webhook is registered against).
- **Body**: raw, channel-specific payload (JSON for WhatsApp; not used for
  email, which never hits this route — see [message flow](message-flow.md)).
- **Response**: `{"status": "accepted"}` on success.
- **Errors**: `404 ConnectionNotFoundError` (unknown connection, or
  `channel_type` mismatch), `401 WebhookSignatureError`.
- **Performance target**: < 2s p99 (ack fast, real work happens in the
  worker — Meta's retry window is ~10s).

## Inbox reads

### `GET /omnichannel/orgs/{org_id}/conversations`

List an org's conversations, most recently active first. Requires
membership only (any role — see [role mapping](../../architecture/auth-and-authorization.md#role-mapping-gap-documented-not-silently-resolved)).

| Query param | Default | Meaning |
|---|---|---|
| `status` | — | Filter `open`/`pending`/`closed` |
| `assigned_user_id` | — | Filter to one agent's inbox |
| `limit` | 50 | Clamped to 100 |
| `offset` | 0 | — |

Returns `list[ConversationSummary]`.

### `GET /omnichannel/orgs/{org_id}/conversations/{conversation_id}`

One conversation's thread — the most recent `limit` (default 50, max 100)
messages, oldest-first, plus signed attachment URLs. Returns
`ConversationDetail`. `404 ConversationNotFoundError` if the conversation
doesn't exist *for this org* (deliberately the same error whether it's
missing or belongs to another org).

### `POST /omnichannel/orgs/{org_id}/conversations/{conversation_id}/read`

Zeroes the conversation's unread counter. A separate POST rather than a
side effect of the GET above — a prefetch or double-render must not
silently clear an agent's unread badge. Returns
`{"conversation_id": str, "unread_count": 0}`.

## Sending & assignment

### `POST /omnichannel/orgs/{org_id}/conversations/{conversation_id}/messages`

Send an agent's reply. Body: `{"body_text": str}`. Requires membership and
role ≠ GUEST/Viewer. Persists as `"queued"` and enqueues for the worker —
does **not** wait for the actual channel send. Returns
`{"message_id": str, "status": "queued"}`.
`429 RateLimitError` if the channel's outbound rate limit is registered and
exceeded.

### `POST /omnichannel/orgs/{org_id}/conversations/{conversation_id}/claim`

Agent claims an unassigned conversation. Idempotent if already the caller's.
`409 ConversationAlreadyAssignedError` if owned by someone else (use
reassign instead). Returns `{"conversation_id", "assigned_user_id"}`.

### `POST /omnichannel/orgs/{org_id}/conversations/{conversation_id}/reassign`

Owner/Admin only. Body: `{"assignee_user_id": str}` — must be an org
member. Returns `{"conversation_id", "assigned_user_id"}`.

### `PUT /omnichannel/orgs/{org_id}/routing-config`

Owner/Admin only. Body: `{"strategy": "manual" | "single_assignee",
"single_assignee_user_id": str | None}`. `400 RoutingError` for an
unsupported strategy (round-robin/sticky are not yet implemented — see
[known limitations](known-issues.md)).

## Realtime

### `GET /omnichannel/orgs/{org_id}/stream`

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
  including Core's own `core_admin`/`health` routers.
- [Message flow](message-flow.md) — what happens after a webhook/reply is
  accepted.
