# A2Z Documentation Index

> The scannable, one-table-per-area map of **every** document in this repo,
> what it covers, and the code or authority it maps to. For a guided
> reading order instead, start at [`README.md`](README.md). If code and a
> doc disagree, the code wins — check
> [known issues](services/omnichannel/known-issues.md) first, then file an
> issue against the doc.

**Legend** — *Authority* = the source of truth a doc is derived from or
defers to. `(spec)` = normative, `(record)` = a decision log, `(ref)` = a
description of current code.

---

## Find it by task

| I want to… | Go to |
|---|---|
| Understand the whole system fast | [architecture/overview.md](architecture/overview.md) → [request-lifecycle.md](architecture/request-lifecycle.md) |
| Know how a request is authenticated/authorized | [architecture/auth-and-authorization.md](architecture/auth-and-authorization.md), [zero-trust.md](zero-trust.md) |
| Call or add an HTTP endpoint | [api-reference.md](api-reference.md) (core) · [services/omnichannel/api-reference.md](services/omnichannel/api-reference.md) (omni) |
| Use a Core module (`app/core/*`) | [core/README.md](core/README.md) → the module's page |
| Know where data lives / how org-scoping is enforced | [architecture/data-flow.md](architecture/data-flow.md) |
| Publish or consume a cross-service event | [events.md](events.md) (catalog) · [architecture/event-driven-architecture.md](architecture/event-driven-architecture.md) (mechanisms) |
| Work on Omni-Channel | [services/omnichannel/README.md](services/omnichannel/README.md) + [known-issues.md](services/omnichannel/known-issues.md) |
| Set an env var / config value | [configuration.md](configuration.md) |
| Run, write, or understand tests | [testing.md](testing.md) |
| Change a DynamoDB shape or a Postgres schema | [migrations.md](migrations.md) |
| Run something locally / a helper script | [scripts.md](scripts.md), [`../README.md`](../README.md) |
| Understand deploy / CI | [architecture/deployment.md](architecture/deployment.md), [ci-cd.md](ci-cd.md), [`../infra/README.md`](../infra/README.md) |
| Know a retention / cost / product decision | [retention.md](retention.md), [cost-notes.md](cost-notes.md), [omnichannel-decisions.md](omnichannel-decisions.md) |
| Work on Invoicing (Phase 2, built) | [`../app/services/invoicing/CLAUDE.md`](../app/services/invoicing/CLAUDE.md) (design + current state) + [phase2-invoicing.md](phase2-invoicing.md) (roadmap) |
| Split the monolith later | [architecture/microservices-distribution.md](architecture/microservices-distribution.md) |

---

## Architecture (`docs/architecture/`)

| Doc | Covers | Authority |
|---|---|---|
| [overview.md](architecture/overview.md) | System components, layer responsibilities, why a monolith | ref |
| [request-lifecycle.md](architecture/request-lifecycle.md) | HTTP → router → Core → response; error-mapping convention | ref |
| [auth-and-authorization.md](architecture/auth-and-authorization.md) | JWT validation, Cognito signup flow, role model, role-vocabulary gap | ref |
| [data-flow.md](architecture/data-flow.md) | What lives in DynamoDB/Postgres/S3/Redis/Secrets Manager; per-store org-scoping | ref |
| [event-driven-architecture.md](architecture/event-driven-architecture.md) | EventBridge vs. Redis pub/sub — two mechanisms, when to use which | ref |
| [deployment.md](architecture/deployment.md) | ECS Fargate control plane vs. Omni-Channel single-EC2 MVP; codified vs. planned | ref |
| [microservices-distribution.md](architecture/microservices-distribution.md) | Forward-looking plan to split the monolith: triggers, phases, Core-as-SDK | record |

## Core platform layer (`docs/core/` → `app/core/`)

| Doc | Module | Backing store |
|---|---|---|
| [core/README.md](core/README.md) | Module index + dependency graph + "extending Core" protocol | — |
| [auth.md](core/auth.md) | `auth` — JWT validation, claims, test-token factory | Cognito JWKS (Redis-cached) |
| [membership.md](core/membership.md) | `membership` — user/org/role CRUD + queries | DynamoDB `a2z-core-membership` |
| [audit.md](core/audit.md) | `audit` — append-only event log + query | DynamoDB `a2z-core-audit` |
| [settings.md](core/settings.md) | `settings` — org config, cached reads, invoice counter | DynamoDB `a2z-core-settings` + Redis |
| [rate-limit.md](core/rate-limit.md) | `rate_limit` — sliding-window limiter | Redis |
| [events-module.md](core/events-module.md) | `events` — cross-service publish | EventBridge `a2z-bus` |
| [storage.md](core/storage.md) | `storage` — S3 up/down, signed URLs, metadata | S3 + DynamoDB `files` |
| [email.md](core/email.md) | `email` — SES send, suppression, status, **domain verification** | SES + DynamoDB `email-events`/`suppression` |
| [secrets.md](core/secrets.md) | `secrets` — per-org/service credentials, **get + put** | Secrets Manager + Redis |
| [realtime.md](core/realtime.md) | `realtime` — fan-out to connected clients | Redis pub/sub |
| [shared-infrastructure.md](core/shared-infrastructure.md) | `clients`, `logging`, `exceptions`, `_ddb`, `config` | — |

## Omni-Channel service (`docs/services/omnichannel/` → `app/services/omnichannel/`)

| Doc | Covers |
|---|---|
| [README.md](services/omnichannel/README.md) | Service overview + entry point |
| [data-model.md](services/omnichannel/data-model.md) | Postgres tables, conversation/message model |
| [adapters.md](services/omnichannel/adapters.md) | Email/SMS/WhatsApp channel adapters + registry |
| [message-flow.md](services/omnichannel/message-flow.md) | Inbound webhook → routing → inbox → send path |
| [routing-and-realtime.md](services/omnichannel/routing-and-realtime.md) | Assignment/routing rules + SSE stream/presence |
| [api-reference.md](services/omnichannel/api-reference.md) | Every `/v1/omnichannel/*` route |
| **[known-issues.md](services/omnichannel/known-issues.md)** | **Documented drift between the design doc and the implementation — read regardless of task** |

## Invoicing service (Phase 2 — built)

No `docs/services/invoicing/` tree yet (the one open Definition-of-Done item,
CLAUDE.md §16) — design, current state, and roadmap:

| Doc | Covers |
|---|---|
| [`../app/services/invoicing/CLAUDE.md`](../app/services/invoicing/CLAUDE.md) | Authoritative design: data model, HTTP surface, state machine, Core dependency map |
| [phase2-invoicing.md](phase2-invoicing.md) | Short kickoff roadmap / build order |

## Cross-cutting operational references (`docs/`)

| Doc | Covers | Authority |
|---|---|---|
| [api-reference.md](api-reference.md) | Full HTTP surface: health, core admin, versioning, error shape | ref |
| [configuration.md](configuration.md) | Every environment variable + config registry | ref |
| [testing.md](testing.md) | Test layout, moto/fakeredis harness, coverage gates, cross-org isolation | ref |
| [ci-cd.md](ci-cd.md) | GitHub Actions pipeline, job by job | ref |
| [scripts.md](scripts.md) | `create_local_resources.py`, `build_lambda.sh`, Docker, migration scripts | ref |
| [migrations.md](migrations.md) | DynamoDB additive-change rules vs. Alembic for Postgres | spec |
| [events.md](events.md) | Current EventBridge event catalog (wire contract) | spec |
| [zero-trust.md](zero-trust.md) | Zero Trust API policy: per-request verification, endpoint classes, checklist | spec |

## Standing decision records (`docs/`)

| Doc | Records |
|---|---|
| [retention.md](retention.md) | TTL/lifecycle policy per data store |
| [cost-notes.md](cost-notes.md) | AWS cost posture and thresholds to revisit |
| [omnichannel-decisions.md](omnichannel-decisions.md) | Product/engineering decisions for Omni-Channel |
| [phase2-invoicing.md](phase2-invoicing.md) | Kickoff roadmap for the Invoicing service (v1 built — see `app/services/invoicing/CLAUDE.md`) |
| [potential-additions.md](potential-additions.md) | Candidate future Core capabilities |

## Authoritative specs (outside `docs/`)

| Doc | Role | Authority |
|---|---|---|
| [`../CLAUDE.md`](../CLAUDE.md) | Build conventions, the five gaps, cost/retention decisions, build order | spec (process) |
| [`../A2Z_Core_Design_TestPlan.md`](../A2Z_Core_Design_TestPlan.md) | Core API signatures, schemas, test scenarios | spec (Core API) |
| [`../app/services/omnichannel/CLAUDE.md`](../app/services/omnichannel/CLAUDE.md) | Omni-Channel's original build plan — historical design record | record |
| [`../infra/README.md`](../infra/README.md) | Terragrunt module reference + apply instructions | ref |
| [`../README.md`](../README.md) | Repo root quickstart | ref |

---

*Every diagram in this tree is Mermaid, rendered natively by GitHub. When
you add or rename a doc, add its row here and in [`README.md`](README.md).*
