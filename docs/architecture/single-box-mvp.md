# Single-Box MVP — Deployment Shape & What Changed

> Part of the [architecture reference](../README.md). See also: [deployment.md](deployment.md), [`../../infra/README.md`](../../infra/README.md), [cost-notes.md](../cost-notes.md).
> **Authority:** _reference_ — describes current code and infra; if the two disagree, the code wins.

## Why this exists

A2Z Core was originally built for a Year-12 target (~1K orgs, ~3M emails/mo,
CLAUDE.md §10): ECS Fargate behind an ALB, ElastiCache, RDS, a NAT gateway.
At one user, that shape cost ~$130–140/mo for infrastructure a single
customer's traffic will never approach. This doc records what changed to
collapse it to one EC2 instance at ~$22/mo, and — more importantly — the
constraints that collapse introduces, so nobody re-adds a second process or
instance without first reading the "Single-process is load-bearing" section
below.

## What moved in-process (no more Redis, anywhere)

Redis backed three genuinely different things, not just caching. Each moved
to a different replacement:

| Was (Redis) | Now | Module |
|---|---|---|
| Sliding-window rate limiter (sorted set) | In-process `dict[tuple, deque[float]]` on `time.monotonic()` | `core.rate_limit` |
| Pub/sub fan-out for realtime updates | In-process `asyncio.Queue`-per-subscriber broker | `core.realtime` |
| 5-min org-settings cache | Deleted outright — reads go straight to DynamoDB (well inside the `< 50ms` target) | `core.settings` |
| 5-min secrets cache, 1h media signed-URL cache | `core.cache.TTLCache` — same TTL semantics, in-process dict instead of a Redis key | `core.secrets`, `app/services/omnichannel/media.py` |
| 24h "SES config set exists" cache | Process-lifetime `set[str]` (config sets never expire once created, so there's nothing to TTL) | `core.email` |
| Agent presence (`presence.py`) | **Deleted**, not ported — it had zero production callers (auto-routing/presence is a deferred v1.5 feature, §15 of `app/services/omnichannel/CLAUDE.md`). The Postgres `presence` table and model stay; a future implementation reads/writes them directly with a freshness window standing in for the old TTL. | — |
| JWKS cache (`core.auth`) | Unchanged — this was **already in-process**, by deliberate design predating this collapse (see `docs/architecture/auth-and-authorization.md`) | `core.auth` |

`tests/conftest.py`'s `fake_redis` fixture is gone; an autouse
`_reset_core_state` fixture clears the in-process broker, rate-limit
windows, and TTL caches between tests instead — the same isolation
guarantee, different mechanism.

## The worker that never existed

`app/services/omnichannel/worker.py` defined `process_inbound_batch` /
`process_outbound_batch`, but **nothing ever called them outside tests** —
no `__main__`, no loop, no second container. The Dockerfile has always had
exactly one `CMD` (uvicorn). In the originally-planned shape this was meant
to run as a second ECS task / EC2 process; that second process was never
actually built.

Rather than build the missing second process, `worker.run_forever()` now
runs as a **FastAPI lifespan background task inside the same process** as
the API (see `app/main.py`'s `lifespan`), gated behind
`RUN_OMNICHANNEL_WORKER` (default off, so the entire test suite — which
boots the app via `TestClient` constantly — never races a live worker
against mocked AWS state). The real deployment turns it on via
`user-data.sh`.

This was the *only* correct choice once Redis moved in-process (see next
section), and it happens to also close a real gap: Omni-Channel inbound
messaging had never run end-to-end outside tests until this shipped.

## Single-process is load-bearing, not a preference

`core.realtime` and `core.rate_limit` are plain Python module-global state
with **no cross-process transport**. This was fine when there was only ever
going to be one process (the API), but it means:

- **`--workers 2`** would give each uvicorn worker its own realtime broker —
  an SSE stream connected to worker A would never see an update published
  by a request served on worker B.
- **A second OS process** (the "real" worker the original plan called for)
  would give itself its own rate-limit windows — every configured limit,
  including `omnichannel.whatsapp.send`'s Meta pair-rate ceiling, would
  silently double.

The Dockerfile's `CMD` pins `--workers 1` with an inline comment stating
this. Revisit this whole section — not just the flag — before ever running
more than one process: either move the broker/limiter onto something with
real cross-process transport (Postgres `LISTEN`/`NOTIFY` was considered and
rejected for `core.realtime` specifically because Core has zero Postgres
dependency by design and `NOTIFY` only fires on commit — see git history on
`core/realtime.py` for the full reasoning) or accept re-adding Redis.

## Lambdas → in-process

Both out-of-band Lambdas are gone; both jobs moved into the one app
process:

- **`cognito_post_confirm`** → `app/dependencies.py::current_user` now calls
  `membership.create_user_if_not_exists` on every authenticated request
  (skipped after the first success per user id, via an in-process
  "already provisioned" set). CLAUDE.md §5 already preferred bootstrapping
  on first authenticated request over the Lambda; there's no Lambda left to
  prefer it *over* anymore.
- **`ses_notifications`** → `app/routers/ses_notifications.py`, an HTTPS
  route SNS delivers to directly (`aws_sns_topic_subscription` with
  `protocol = "https"`). An IAM-invoked Lambda is reachable by SNS and
  nothing else, by construction; an HTTP endpoint is reachable by anyone
  who finds the URL. This route therefore **verifies SNS's message
  signature** (RSA, fetching and caching the signing cert, host-allowlisted
  to `sns.*.amazonaws.com`) before trusting any payload — without that, an
  attacker could poison an org's suppression list. See the module's own
  docstring for the full threat model.

`scripts/build_lambda.sh` and `app/lambdas/` are deleted.

## Infra collapse

- `infra/modules/{ecs,redis,rds}/` deleted outright — Fargate/ALB/
  autoscaling, ElastiCache, RDS.
- `infra/modules/vpc/` trimmed to one public subnet, one AZ, no NAT gateway,
  one security group (the free DynamoDB/S3 gateway endpoints stayed).
- `infra/modules/iam/` collapsed to one EC2 instance role covering
  everything the app touches (DynamoDB/S3/SES/EventBridge/SQS/Secrets
  Manager/ECR-pull) — there's only one process to grant permissions to now.
- `infra/modules/cognito/` lost its Lambda + `lambda_config` wiring.
- New: `infra/modules/ecr/` (the one image repo — pulled out on its own to
  avoid a dependency cycle between `iam` and `ec2-simple`) and
  `infra/modules/ses-notifications-subscription/` (the SNS→HTTPS
  subscription — also its own module, needing both `ses`'s topic ARN and
  `ec2`'s public endpoint, which would otherwise make those two modules
  depend on each other).
- `infra/modules/ec2-simple/` runs the app + a Postgres container + Caddy
  (TLS termination) via docker-compose, all supervised by one systemd unit.

See [`../../infra/README.md`](../../infra/README.md) for the full module
layout, apply order, and the cost table.

## What this does *not* solve

- **No RDS safety net.** The nightly `pg_dump -> S3` cron in
  `user-data.sh` is the only Postgres backup. It has not been restore-tested.
  This is the single highest-risk open item — see
  [`../../infra/README.md`](../../infra/README.md)'s cost section and
  `app/services/omnichannel/CLAUDE.md` §16.
- **No HA.** One instance, one AZ, one Postgres. A box reboot takes down
  webhook endpoints; providers retry with backoff so brief deploys are fine,
  extended downtime loses messages.
- **EventBridge stays**, deliberately, as the one exception to "remove
  what's unused": it has 14 publish call sites and zero subscribers today,
  but it's ~$0 and is the documented seam Phase 2's commission-attribution
  feature depends on (§6.3 of `app/services/omnichannel/CLAUDE.md`).
