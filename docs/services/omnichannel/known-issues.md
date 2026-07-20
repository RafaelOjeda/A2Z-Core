# Known Issues & Design-vs-Implementation Drift

> Part of the [Omni-Channel service docs](README.md). This page exists specifically because `app/services/omnichannel/CLAUDE.md` (the service's original build plan) and the actual code have diverged in a few places since it was last updated. Per the audit instructions this documentation tree follows: derive behavior from the code, not the plan, when the two disagree — and record the disagreement here rather than silently picking one.

## 1. SMS adapter is fully built but not registered

`app/services/omnichannel/adapters/sms.py` is a complete `ChannelAdapter`
implementation (outbound via AWS SNS SMS, inbound normalization, delivery
webhook parsing) with real logic and a provider decision recorded in
[`docs/omnichannel-decisions.md`](../../omnichannel-decisions.md). However:

- It is **not** in `adapters/registry.py::_REGISTRY` — `get_adapter("sms")`
  raises `ChannelAdapterError`.
- It takes an `org_id` constructor argument (`SmsAdapter(org_id)`), unlike
  every other adapter, which is a stateless singleton — it couldn't be
  dropped into the registry's `dict[str, ChannelAdapter]` shape (which
  holds pre-built instances) without either changing its constructor or
  changing the registry to support per-call construction.
- No `"omnichannel.sms.send"` entry exists in `app/config.py::RATE_LIMITS`.
- `models.ChannelType.SMS` exists as an enum value, but no
  `channel_connections` row could ever be created for it through any
  current API path — including the connections CRUD API added in the
  2026-07-18 API review (`POST /v1/omnichannel/orgs/{org_id}/connections`
  explicitly rejects `channel_type="sms"` with `400
  ConnectionValidationError`, for exactly this reason).

The service's own design doc (§15, channel scope revision) states "SMS is
cut from v1" as a deliberate decision — this is consistent with SMS not
being *registered*, but it does not explain why a fully-implemented adapter
exists in the tree unregistered. Most likely explanation: the adapter was
built ahead of the registry wiring and the wiring step was never finished
or was intentionally left as a "ready to flip on" artifact. Either way,
**do not assume SMS works** because the file exists — verify against the
registry, not the adapter file, when answering "which channels are live."

## 2. Presence is fully implemented despite being described as "deferred"

`app/services/omnichannel/CLAUDE.md` §5.3/§15 groups presence with
round-robin/sticky auto-routing under "deferred — not built for v1." In
fact, `presence.py` is a complete, tested Redis-backed implementation
(`heartbeat`, `get_status`, `list_online_agents`), with unit test coverage.

What's actually true: **the routing strategies that would consume presence
(round-robin, sticky) are genuinely not built** — `routing.py` only
implements `manual` and `single_assignee`, and `set_routing_config` rejects
any other strategy string with `RoutingError`. Presence itself works and is
tested, but nothing in the current API surface calls `heartbeat` or
`list_online_agents` from a router or the worker — it's reachable only via
direct import (as a test does) or by a future caller. Treat presence as
"built, integrated nowhere yet," not "not built."

## 3. Duplicate/orphaned Alembic migration — RESOLVED 2026-07-20

The orphaned duplicate root (`0001_initial_schema.py`) was **deleted** on
2026-07-20. `migrations/versions/` now holds a single linear chain —
`0001_baseline_schema.py` (rev `1bfacee578a4`) →
`0002_inbox_index_desc_nulls_last.py` → `0003_message_client_dedup_key.py` —
so `alembic upgrade head` is unambiguous and was verified end to end against
a fresh Postgres 16 (`<base>` → `1bfacee578a4` → `0002` → `0003`, 10 tables).
Nothing had ever been stamped with the orphan's revision (no real AWS/RDS
apply has happened), so the deletion was safe. See
[data model: Migrations](data-model.md#migrations). *Historical note: this
was previously ambiguous (two heads); the workaround was to target
`alembic upgrade 0003_message_dedup_key` explicitly.*

## 4. RDS Terraform module exists ahead of both phases that would use it

`infra/modules/rds/` and `infra/live/prod/rds/` are fully codified, but:

- [`docs/phase2-invoicing.md`](../../phase2-invoicing.md) still lists "new
  `infra/modules/rds/`" as a Phase 2 (Invoicing) to-do that hasn't started.
- `app/services/omnichannel/CLAUDE.md` §12 explicitly defers RDS to
  Omni-Channel's future "distribution phase" (MVP uses an on-box Postgres
  container).
- Nothing in `docker-compose.yml` or CI points at this RDS module — the
  service's actual Postgres today is the `postgres` container.

See [deployment architecture](../../architecture/deployment.md#whats-actually-codified-in-infra-today)
for the full infra-codification-vs-plan table.

## 5. Role vocabulary gap (by design, not a bug)

The service's design doc uses Owner/Admin/Agent/Viewer; `core.membership.Role`
only has OWNER/ADMIN/MEMBER/GUEST. This is explicitly not a defect — see
[auth & authorization](../../architecture/auth-and-authorization.md#role-mapping-gap-documented-not-silently-resolved) —
but it's easy to misread code that checks `role == Role.GUEST` as "viewers
are blocked" without realizing `GUEST` *is* how this service spells
"Viewer."

## 6. Deferred features confirmed still absent (as of this audit)

These match what the design doc says and are confirmed accurate by reading
the code — listed here for completeness, not as new findings:

- **Commission attribution** — tables ship, no code consumes them
  (`invoice.paid` has no producer).
- **Templates** — `templates` table ships, unused; WhatsApp outbound is
  reply-within-24h, text-only.
- **AI features** (Bedrock summaries, suggested replies) — entirely absent,
  cut from scope.
- **Round-robin / sticky routing** — not implemented (see #2 above).
- **Nightly Postgres backup + tested restore** — not yet exercised; no AWS
  account/EC2 host to run it against. Called "non-negotiable" in the
  service's own design doc; still the single highest-risk open item before
  any production launch on the single-EC2 MVP shape.
- **X-Ray + CloudWatch alarms** — the metric series they'd watch are
  emitted and tested (`metrics.py`); the alarms themselves need a real AWS
  account to create.
- **Public Inbox API** — deferred, no external consumer demand yet.

## What this means for anyone extending the service

Before assuming a capability is (or isn't) live, check the actual registry
/ wiring, not just the presence of a file or a line in the design doc:

- Channel live? → check `adapters/registry.py::_REGISTRY`, not
  `adapters/` directory contents.
- Routing strategy live? → check `routing.py::_SUPPORTED_STRATEGIES`.
- Migration chain valid? → check `down_revision` links, not just filenames.
- Infra module applied? → check `infra/live/prod/`, not just
  `infra/modules/`.
