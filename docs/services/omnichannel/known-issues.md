# Known Issues & Design-vs-Implementation Drift

> Part of the [Omni-Channel service docs](README.md). This page exists specifically because `app/services/omnichannel/CLAUDE.md` (the service's original build plan) and the actual code have diverged in a few places since it was last updated. Per the audit instructions this documentation tree follows: derive behavior from the code, not the plan, when the two disagree — and record the disagreement here rather than silently picking one.
> **Authority:** _record_ — a dated decision/log, not a live description of current code.

## 1. SMS adapter is fully built but not registered

`app/services/omnichannel/adapters/sms.py` is a complete `ChannelAdapter`
implementation (outbound via AWS SNS SMS, inbound normalization, delivery
webhook parsing) with real logic and a provider decision recorded in
[`docs/omnichannel-decisions.md` #7](../../omnichannel-decisions.md).
However:

- It is **not** in `adapters/registry.py::_REGISTRY` — `get_adapter("sms")`
  raises `ChannelAdapterError`.
- It takes an `org_id` constructor argument (`SmsAdapter(org_id)`), unlike
  every other adapter, which is a stateless singleton — it couldn't be
  dropped into the registry's `dict[str, ChannelAdapter]` shape (which
  holds pre-built instances) without either changing its constructor or
  changing the registry to support per-call construction.
- No `"omnichannel.sms.send"` entry exists in `app/config.py::RATE_LIMITS`.
- `tests/unit/omnichannel/test_registry.py::test_registry_contents_exact`
  asserts `_REGISTRY`'s keys are exactly `{email, whatsapp, messenger,
  instagram}` (2026-08 hardening pass) — this is now a build-breaking test,
  not just a doc claim, if SMS (or anything else) is ever added without
  going through the checklist in [adapters.md](adapters.md#extension-points).
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

## 2. Presence — RESOLVED (deleted) 2026-07-27

This item used to document that `presence.py` (a Redis-backed
`heartbeat`/`get_status`/`list_online_agents` implementation) existed
despite the design doc grouping presence with round-robin/sticky
auto-routing under "deferred — not built for v1." That drift is gone now,
not just documented: when the platform collapsed to the single-box MVP and
Redis was removed entirely
([single-box-mvp.md](../../architecture/single-box-mvp.md)), `presence.py`
was **deleted outright** rather than ported, because it had zero
production callers — nothing in a router or the worker ever called
`heartbeat`/`list_online_agents`; only its own unit tests exercised it. The
design doc's "deferred" was accurate all along for the feature; only the
unused module's existence was the drift, and it no longer exists.

The `presence` Postgres table and model **stay** (cost nothing, already in
the Alembic baseline) for when auto-routing (round-robin/sticky) is
actually built — routing.py still only implements `manual` and
`single_assignee`. A future implementation should read/write that table
directly rather than reintroducing Redis for one module — see
[single-box-mvp.md](../../architecture/single-box-mvp.md) for the
recommended shape (a freshness window on `updated_at` standing in for the
old TTL).

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

## 4. RDS Terraform module — RESOLVED (deleted) 2026-07-27

This item used to document that `infra/modules/rds/` was fully codified
ahead of any phase actually needing managed Postgres. It's moot now: the
module was **deleted** when the platform collapsed to the single-box MVP
([single-box-mvp.md](../../architecture/single-box-mvp.md)) — Postgres runs
as a container next to the app on the one EC2 instance, permanently, not as
a placeholder for a future RDS migration. `docker-compose.yml` and CI's
`postgres` service container remain the actual Postgres both locally and in
the deployed shape.

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
- **Templates** — `templates` table ships, unused; every Meta channel's
  outbound is reply-within-24h, text-only (not just WhatsApp — see
  [adapters.md](adapters.md)).
- **AI features** (Bedrock summaries, suggested replies) — entirely absent,
  cut from scope.
- **Round-robin / sticky routing** — not implemented (see #2 above).
- **Nightly Postgres backup + tested restore** — **partially resolved.**
  `restore-postgres.sh` (`infra/modules/ec2-simple/files/`) now exists
  alongside the nightly `backup-postgres.sh` cron, and
  `tests/integration/backup/test_restore_drill.py` runs both real scripts
  end to end against throwaway databases on every CI run, asserting the
  restored data is byte-for-byte faithful (numeric precision, timezone-aware
  timestamps, JSONB, unique constraints), not just present. What remains
  genuinely untested: nobody has drilled a restore against the *deployed*
  box, because no AWS account exists to deploy one — see
  [`../../../DEPLOYMENT.md`](../../../DEPLOYMENT.md)'s backup runbook for
  that procedure. Called "non-negotiable" in the service's own design doc;
  the on-box drill is still the single highest-risk open item before any
  production launch on the single-EC2 MVP shape.
- **X-Ray + CloudWatch alarms** — moot on the current single-box MVP:
  `metrics.py` emits structured log lines, not CloudWatch custom metrics
  (there's no agent shipping logs off-box either — see
  [single-box-mvp.md](../../architecture/single-box-mvp.md)). Reintroducing
  a real metrics backend behind the same `record_*` function names is the
  starting point if this is ever needed again.
- **Public Inbox API** — deferred, no external consumer demand yet.
- **Outbound media (agent-sent attachments)** — no adapter sends anything
  but text; no attachment-upload API exists for replies either.
- **Inbound media on WhatsApp/Messenger/Instagram** — still a placeholder
  body, not downloaded (unchanged by item 7 below; see
  [adapters.md](adapters.md)).

## 7. Delivery statuses, email error-wrapping, connection nondeterminism — RESOLVED 2026-08-14

Three related gaps in the 4 live channels' outbound/delivery paths,
closed in the same hardening pass:

- **Delivery statuses were never applied.** Every adapter implemented
  `interpret_delivery_webhook`, but nothing called it — no message ever
  advanced past `sent`; `MessageStatus.DELIVERED`/`READ` were unreachable
  in production. The worker now calls it unconditionally alongside
  `normalize_inbound` on every inbound payload and applies updates
  monotonically. See [message flow: delivery status updates](message-flow.md#delivery-status-updates).
- **Email send failures escaped the retry path.** `core.email.send_email`
  raises `core.exceptions.CoreError` subclasses (`SuppressionListError`,
  `RateLimitError`, ...), which are not `ChannelAdapterError` — they
  escaped `worker.py`'s `except ChannelAdapterError` entirely instead of
  following the normal redeliver/mark-failed path. `EmailAdapter.send_outbound`
  now wraps them.
- **`_find_connection` was nondeterministic** (`.first()` with no filter or
  order) and could select a *disabled* connection for an outbound send. Now
  filters to `status == "active"` and orders by `created_at`
  (oldest-active-wins); an org with 2+ active connections on one channel
  still doesn't get per-conversation pinning — that's a separate, deliberate
  deferral, not a bug this pass claims to fix.

Also corrected in the same pass: `MessengerPlatformAdapter.supported_features.read_receipts`
was `True` despite the adapter structurally being unable to emit read
updates (Messenger's `read` webhook event is watermark-only, no
per-message id) — now `False`, matching WhatsApp's honest `True` (WhatsApp's
status webhook *does* carry per-message read events). Graph API response
parsing (`whatsapp.py`/`messenger.py`) also gained a guard against a
malformed 2xx body raising an unhandled `KeyError`/`IndexError` instead of
`ChannelAdapterError`.

None of this changed which channels are registered or what media
capabilities exist — see item 6 above, unchanged by this pass.

## What this means for anyone extending the service

Before assuming a capability is (or isn't) live, check the actual registry
/ wiring, not just the presence of a file or a line in the design doc:

- Channel live? → check `adapters/registry.py::_REGISTRY`, not
  `adapters/` directory contents.
- Routing strategy live? → check `routing.py::_SUPPORTED_STRATEGIES`.
- Migration chain valid? → check `down_revision` links, not just filenames.
- Infra module applied? → check `infra/live/prod/`, not just
  `infra/modules/`.
