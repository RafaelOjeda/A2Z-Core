# Routing, Assignment, Presence & Realtime Inbox

> Part of the [Omni-Channel service docs](README.md). Source: [`routing.py`](../../../app/services/omnichannel/routing.py), [`presence.py`](../../../app/services/omnichannel/presence.py), [`stream.py`](../../../app/services/omnichannel/stream.py).

## v1 scope

Manual claim/reassign plus one auto-strategy (single-assignee). Round-robin
and sticky routing are designed but **deferred** — see
[known limitations](known-issues.md) for the gap between "deferred" as
stated in the design doc and what's actually implemented (`presence.py` is
fully built, even though presence/auto-routing are described together as
deferred).

## Assignment state machine

```mermaid
stateDiagram-v2
    [*] --> Unassigned: conversation created
    Unassigned --> Assigned: claim(user) / single_assignee auto-apply
    Assigned --> Assigned: reassign(new_user) [Owner/Admin only]
    Assigned --> Assigned: claim(same_user) [idempotent no-op]
    Assigned --> Conflict: claim(different_user) while already assigned
    Conflict --> [*]: raises ConversationAlreadyAssignedError (409)
```

Every transition into "Assigned" writes through one shared internal helper
(`_record_assignment`), which — regardless of whether the trigger was
`claim`, `reassign`, or the single-assignee auto-apply — always does all
four of:

```mermaid
flowchart LR
    Assign["_record_assignment(...)"] --> Row["INSERT ConversationAssignment\n(append-only history row)"]
    Assign --> Audit["core.audit.log_audit\n('conversation.assigned')"]
    Assign --> Event["core.events.publish_event\n('conversation.assigned', source=a2z.omnichannel)"]
    Assign --> RT1["core.realtime.publish_update\norg:{org_id}:conversations"]
    Assign --> RT2["core.realtime.publish_update\nuser:{assignee}:notifications"]
```

This uniformity is what makes commission attribution replayable later
(§5.5 in the design doc) — the assignment history doesn't care which path
produced a row.

## Public API (`routing.py`)

| Function | Who can call | Behavior |
|---|---|---|
| `claim(session, org_id, conversation_id, user_id)` | Owner/Admin/Agent (not Viewer/GUEST) | Idempotent if caller already owns it; raises `ConversationAlreadyAssignedError` (409) if assigned to someone else |
| `reassign(session, org_id, conversation_id, actor_user_id, assignee_user_id)` | Owner/Admin only | Validates the new assignee is actually an org member first |
| `apply_single_assignee_if_configured(session, conversation)` | Called only by the worker, only for a **brand-new** conversation | No-ops unless the org has opted into `single_assignee` routing |
| `set_routing_config(org_id, actor_user_id, strategy, single_assignee_user_id=None)` | Owner/Admin only | `strategy` must be `"manual"` or `"single_assignee"` — anything else raises `RoutingError` (400 — a request-validation failure, not a 500; see `exceptions.py`) |

### Routing configuration

Stored in `core.settings`' free-form `metadata` field, namespaced
`metadata["omnichannel"] = {"routing_strategy": ..., "single_assignee_user_id": ...}`
— Core's settings schema is fixed, and `metadata` is exactly the escape
hatch it provides for service-specific config (Design §2.6), so this
required **no** Core change.

## Presence (`presence.py`)

```mermaid
sequenceDiagram
    participant Client
    participant Presence as presence.py
    participant Redis
    participant PG as Postgres (backup)

    Client->>Presence: heartbeat(org_id, user_id, "online") every 20-30s
    Presence->>Redis: SET presence:{org_id}:{user_id} "online" EX 60
    Presence->>PG: UPSERT Presence row (backup/audit only)
    Note over Client: Tab closes -- no explicit signal needed
    Note over Redis: Key simply expires after 60s -> "offline"
```

`get_status`/`list_online_agents` read **only Redis** — the Postgres
`Presence` row is a backup/audit write an operator can inspect after a
Redis flush ("who was online last"), never read on a hot path.

**This module is fully implemented**, despite the service's own design doc
(`app/services/omnichannel/CLAUDE.md` §5.3, §15) listing presence as
"deferred with auto-routing" — see
[known limitations](known-issues.md) for what this means in practice
(nothing currently calls `heartbeat`/`list_online_agents` from a router or
the worker, so the code is exercised only by its own unit tests today).

## Realtime inbox (SSE)

```mermaid
sequenceDiagram
    autonumber
    participant Browser
    participant Router as GET /v1/omnichannel/orgs/{org_id}/stream
    participant Auth as core.auth / core.membership
    participant Stream as stream.py
    participant Redis

    Browser->>Router: GET .../stream?access_token=<jwt>
    Router->>Auth: validate_jwt(token) [query param, since EventSource can't set headers]
    Router->>Auth: membership.get_membership(user_id, org_id)
    alt not a member
        Router-->>Browser: 404 NotFoundError
    end
    Router->>Stream: stream_events(org_id, user_id, is_disconnected=request.is_disconnected)
    Stream->>Redis: SUBSCRIBE rt:org:{org_id}:conversations, rt:user:{user_id}:notifications
    Stream-->>Browser: SSE ": connected"
    loop until disconnect or 5min lifetime cap
        alt message published
            Redis-->>Stream: pub/sub message
            Stream-->>Browser: SSE "data: {...}"
        else idle 15s
            Stream-->>Browser: SSE ": keepalive"
        end
    end
    Stream->>Redis: UNSUBSCRIBE + close (always, in a finally block)
```

Key design choices, each deliberate:

1. **The relay is service-owned, not Core.** `core.realtime.publish_update`
   stops at the Redis `PUBLISH`; `stream.py` is Omni-Channel's own
   subscribe-and-relay-to-SSE code. At the AppSync distribution phase this
   entire module disappears — browsers subscribe to AppSync directly — so
   it's deliberately MVP-only glue, not exported as a Core capability.
2. **The `rt:{channel}` prefix is duplicated, not imported**, from
   `core.realtime` — see
   [event-driven architecture](../../architecture/event-driven-architecture.md#redis-pubsub--realtime-ui-fan-out)
   for why, and the round-trip test that guards against drift.
3. **Auth is inline**, not the shared `CurrentUser` FastAPI dependency —
   because a browser `EventSource` cannot set an `Authorization` header.
   The token arrives as the `access_token` query parameter (a header is
   still accepted as a fallback for non-browser/proxied clients).
   Membership is re-checked on **every** (re)connect, so a revoked
   member's stream ends the next time their browser reconnects.
4. **Idle-tab backpressure**: a 15s heartbeat keeps the connection alive
   through proxies and gives the loop a regular tick to notice a
   disconnected client or an elapsed lifetime; a hard 5-minute
   `max_lifetime_seconds` cap bounds server resource use and caps how long
   a revoked member's stream can outlive the revocation even without a
   client-side reconnect.

## Commission attribution (deferred)

**Not built** — `invoice.paid` has no producer yet (Invoicing doesn't
exist). The rule is locked in the design doc so it isn't "simplified" once
implemented: **snapshot the assigned agent at invoice-creation time, not
payment time** — the agent who did the selling gets credit even if the
conversation is later reassigned or payment arrives weeks later. The
`commission_rules`/`commission_attributions` tables already ship in the
schema (see [data model](data-model.md)) precisely so this becomes
subscriber-only work once Phase 2 lands.

## Security considerations

- `claim`/`reassign`/`set_routing_config` all check membership and role
  before touching a conversation — see
  [auth & authorization](../../architecture/auth-and-authorization.md#role-mapping-gap-documented-not-silently-resolved)
  for the Owner/Admin/Agent/Viewer ↔ OWNER/ADMIN/MEMBER/GUEST mapping this
  module relies on.
- The SSE endpoint re-checks membership on every reconnect specifically so
  a revoked member can't keep an old stream open indefinitely.

## Known limitations

See [`known-issues.md`](known-issues.md) — presence and single-assignee
routing are real and tested, but round-robin/sticky routing are not built,
and nothing in the current codebase calls `presence.heartbeat` from a
router (it's reachable only by direct call or test today).
