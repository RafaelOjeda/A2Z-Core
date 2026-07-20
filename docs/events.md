# Event Catalog

> Part of the [documentation index](README.md). See also: [event-driven architecture](architecture/event-driven-architecture.md) (the mechanism this catalog is the contract for), [`core.events` reference](core/events-module.md), [Omni-Channel message flow](services/omnichannel/message-flow.md).
> **Authority:** _spec_ — normative; code is expected to conform to this.

Cross-service communication is **events only** (CLAUDE.md §6). Core owns the
publisher (`app/core/events.py`); services own their subscribers (later phases).

- **Bus:** single custom EventBridge bus `a2z-bus`.
- **`source`** namespaces the producer: `a2z.core`, `a2z.invoicing`, `a2z.omnichannel`.
- **`detail-type`** = the dotted `event_type`.
- **`detail`** always includes `org_id` so subscribers can scope.
- Payloads are versioned implicitly by `event_type`; breaking shapes get a `v2` suffix.

## Events produced by Core (`source = a2z.core`)

| event_type | When | Key `detail` fields |
|---|---|---|
| `member.added` | A user is added to an org | `org_id`, `user_id`, `role`, `inviter_id` |
| `member.removed` | A user is removed from an org | `org_id`, `user_id`, `remover_id` |
| `member.role_changed` | A member's role changes | `org_id`, `user_id`, `old_role`, `new_role` |
| `email.bounced` | SES hard bounce processed | `org_id`, `email`, `bounce_type`, `message_id` |
| `email.complained` | SES complaint processed | `org_id`, `email`, `message_id` |
| `settings.changed` | Org settings updated | `org_id`, `changed_fields` |

> Subscribers are **not** built in Phase 1 — only the publisher is. Services add
> their own producers (`invoice.*`, etc.) and document them here as they land.

## Events produced by Omni-Channel (`source = a2z.omnichannel`)

| event_type | When | Key `detail` fields |
|---|---|---|
| `message.received` | An inbound message (any channel) is persisted | `org_id`, `conversation_id`, `message_id`, `channel_type` |
| `message.sent` | An outbound message is successfully sent through its channel adapter | `org_id`, `conversation_id`, `message_id` |
| `conversation.assigned` | A conversation is claimed, reassigned, or auto-assigned | `org_id`, `conversation_id`, `assigned_user_id`, `assigned_by`, `reason` |
| `connection.created` | A channel connection is registered (`POST /v1/omnichannel/orgs/{org_id}/connections`) | `org_id`, `connection_id`, `channel_type` |
| `connection.disabled` | A channel connection is soft-disabled (`DELETE .../connections/{connection_id}`) | `org_id`, `connection_id`, `channel_type` |

`message.*` are published by `app/services/omnichannel/worker.py` as part of the
inbound/outbound message flow (`app/services/omnichannel/CLAUDE.md` §5.6, Build
Order Step 5). `conversation.assigned` is published by
`app/services/omnichannel/routing.py` from every assignment path (Build Order
Step 6/8); its `assigned_by` is either a user id or a `routing:*` marker (e.g.
`routing:single_assignee`), and `reason` is one of `claim` / `reassign` /
`single_assignee`. `connection.*` are published by
`app/services/omnichannel/connections.py` (added in the 2026-07-18 API review
alongside the connections CRUD API).

All fire *after* the Postgres write commits, never before — the database row is
the source of truth; the event is a notification of it.

> **Not to be confused with `core.realtime.publish_update`.** Several of these
> flows also push a realtime update on a similarly-named channel (e.g.
> `conversation.assigned` on `org:{org_id}:conversations`). Those are the
> **UI push** to connected browsers (§5.4) and are *not* EventBridge events —
> they carry no cross-service contract and no subscriber should rely on them.
> The table above is the contract; the realtime channels are presentation.

### Not yet produced

`conversation.invoice_requested` (§6.1) is **not** published yet: it exists to
tell Invoicing to create a draft, and Invoicing (Phase 2) doesn't exist. It
lands with the commission feature, which is deferred for the same reason
(§5.5/§15 — `invoice.paid` has no producer to consume, either).
