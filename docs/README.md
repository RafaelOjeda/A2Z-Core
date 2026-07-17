# A2Z Core — Documentation

This directory is the single source of truth for understanding A2Z Core and
the services built on it. If code and documentation ever disagree, that's a
bug in one of them — check [known limitations](services/omnichannel/known-issues.md)
first for documented cases, and otherwise trust the code and file an issue
against these docs.

**New here?** Read in this order: [architecture overview](architecture/overview.md) →
[request lifecycle](architecture/request-lifecycle.md) →
[auth & authorization](architecture/auth-and-authorization.md) → whichever
module or service you're actually touching.

## Architecture

| Doc | Covers |
|---|---|
| [`architecture/overview.md`](architecture/overview.md) | System components, layer responsibilities, why a monolith |
| [`architecture/request-lifecycle.md`](architecture/request-lifecycle.md) | HTTP request → router → Core → response; the error-mapping convention |
| [`architecture/auth-and-authorization.md`](architecture/auth-and-authorization.md) | JWT validation, Cognito signup flow, the role model, the role-vocabulary gap |
| [`architecture/data-flow.md`](architecture/data-flow.md) | What lives in DynamoDB/Postgres/S3/Redis/Secrets Manager, and how org-scoping is enforced per store |
| [`architecture/event-driven-architecture.md`](architecture/event-driven-architecture.md) | EventBridge vs. Redis pub/sub — two mechanisms, when to use which |
| [`architecture/deployment.md`](architecture/deployment.md) | ECS Fargate control plane vs. Omni-Channel's single-EC2 MVP; what's actually codified vs. planned |

## Core platform layer (`app/core/`)

Start at [`core/README.md`](core/README.md) for the module index and
dependency graph. One doc per module:
[`auth`](core/auth.md) · [`membership`](core/membership.md) ·
[`audit`](core/audit.md) · [`settings`](core/settings.md) ·
[`rate_limit`](core/rate-limit.md) · [`events`](core/events-module.md) ·
[`storage`](core/storage.md) · [`email`](core/email.md) ·
[`secrets`](core/secrets.md) · [`realtime`](core/realtime.md), plus
[`shared-infrastructure.md`](core/shared-infrastructure.md) for
`clients`/`logging`/`exceptions`/`_ddb`/`config`.

## Omni-Channel service (`app/services/omnichannel/`)

Start at [`services/omnichannel/README.md`](services/omnichannel/README.md).
[`data-model.md`](services/omnichannel/data-model.md) ·
[`adapters.md`](services/omnichannel/adapters.md) ·
[`message-flow.md`](services/omnichannel/message-flow.md) ·
[`routing-and-realtime.md`](services/omnichannel/routing-and-realtime.md) ·
[`api-reference.md`](services/omnichannel/api-reference.md) ·
**[`known-issues.md`](services/omnichannel/known-issues.md)** — read this
one regardless of what you're doing; it documents real drift between the
service's design doc and its actual implementation.

## Cross-cutting references

| Doc | Covers |
|---|---|
| [`api-reference.md`](api-reference.md) | Full HTTP surface (health, core admin, Omni-Channel) |
| [`configuration.md`](configuration.md) | Every environment variable and config registry |
| [`testing.md`](testing.md) | Test layout, moto/fakeredis harness, coverage gates, cross-org isolation testing |
| [`ci-cd.md`](ci-cd.md) | The GitHub Actions pipeline, job by job |
| [`scripts.md`](scripts.md) | `create_local_resources.py`, `build_lambda.sh`, Docker, migration scripts |
| [`migrations.md`](migrations.md) | DynamoDB's additive-change rules vs. Alembic for Postgres |
| [`events.md`](events.md) | The current EventBridge event catalog (wire contract) |
| [`retention.md`](retention.md) | TTL/lifecycle policy per data store |
| [`cost-notes.md`](cost-notes.md) | AWS cost posture and thresholds to revisit |
| [`omnichannel-decisions.md`](omnichannel-decisions.md) | Recorded product/engineering decisions for Omni-Channel |
| [`phase2-invoicing.md`](phase2-invoicing.md) | Kickoff roadmap for the not-yet-built Invoicing service |
| [`potential-additions.md`](potential-additions.md) | Candidate future Core capabilities |

## Authoritative specs (outside `docs/`)

- [`../CLAUDE.md`](../CLAUDE.md) — build conventions, the original five
  gaps, cost/retention decisions, build order. Process authority.
- [`../A2Z_Core_Design_TestPlan.md`](../A2Z_Core_Design_TestPlan.md) — API
  signatures, schemas, test scenarios. Spec authority for Core.
- [`../app/services/omnichannel/CLAUDE.md`](../app/services/omnichannel/CLAUDE.md) —
  Omni-Channel's original build plan. Treat this `docs/` tree as the
  current-state reference and that file as the historical design record;
  see [known issues](services/omnichannel/known-issues.md) for where
  they've since diverged.
- [`../infra/README.md`](../infra/README.md) — Terragrunt module reference
  and apply instructions.
- [`../README.md`](../README.md) — repo root quickstart.

## How this tree is organized

```
docs/
├── README.md                        # this file
├── architecture/                    # cross-cutting system design + diagrams
├── core/                            # one doc per app/core/*.py module
├── services/omnichannel/            # one doc per concern of the Omni-Channel service
├── api-reference.md, configuration.md, testing.md, ci-cd.md,
├── scripts.md, migrations.md        # cross-cutting operational references
└── events.md, retention.md, cost-notes.md, omnichannel-decisions.md,
    phase2-invoicing.md, potential-additions.md   # standing decision records
```

Every diagram in this tree is Mermaid, rendered directly by GitHub/most
Markdown viewers — no external tooling required to view them.
