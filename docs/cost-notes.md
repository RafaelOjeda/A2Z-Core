# Cost Notes (AWS)

> Part of the [documentation index](README.md). See also: [deployment architecture](architecture/deployment.md) (what's actually codified vs. applied), [Omni-Channel deployment](services/omnichannel/README.md) (its own MVP cost model lives in `app/services/omnichannel/CLAUDE.md` §10/§12).

A2Z optimizes for *low fixed cost while volume is small and spiky*. Decisions
here are deliberate (CLAUDE.md §10) and revisited only when measured spend
crosses the stated thresholds.

## DynamoDB — on-demand (PAY_PER_REQUEST)

All Core tables use **on-demand** billing. No capacity planning while volume is
low; we pay per request. Set in one place: `DDB_BILLING_MODE` in `app/config.py`.

**Threshold to revisit:** if monthly DynamoDB spend crosses **~$100**, evaluate
provisioned capacity + autoscaling for the hot tables (audit, email-events).

## Retention via TTL, not cleanup jobs

TTL attributes are written at insert time (see `docs/retention.md`). DynamoDB and
S3 lifecycle delete expired items for free — we never run (and pay for) scan-based
cleanup jobs.

## Logging — lean by design

CloudWatch is billed per GB ingested. Core logs **significant events only**, one
compact JSON line each. Hot-path reads do not log. `A2Z_LOG_LEVEL=DEBUG` is for
active debugging only — never the default in dev or prod.

## Rough monthly estimate (~1K orgs / ~3M emails, Year-12 target)

| Component | Mode | Est. monthly |
|---|---|---|
| DynamoDB (all core tables) | on-demand | ~$25–40 |
| SES | $0.10 / 1k emails | ~$300 (mostly customer-driven) |
| RDS Postgres (single-AZ t4g.micro) | provisioned | ~$15–25 |
| ECS Fargate (web + worker) | provisioned, autoscale | ~$20–40 |
| ElastiCache Redis (t4g.micro) | provisioned | ~$12–15 |
| S3 (logos + transient media) | standard + lifecycle | ~$1–5 |
| EventBridge | $1 / million events | ~$1 |
| CloudWatch | free tier + overage | ~$0–5 |
| NAT gateway (single, one AZ) | hourly + per-GB | ~$32 + data |
| ALB | hourly + LCU | ~$16–20 |
| Lambda (post-confirm, ses-notifications) | per-invoke | ~$0 at MVP volume |

## Networking cost posture (infra/modules/vpc)

- **One NAT gateway**, single AZ — the ~$32/mo floor. No per-AZ NAT until an
  availability incident actually justifies it (§14: no multi-AZ gold-plating).
- **Free gateway VPC endpoints for DynamoDB and S3** carry the bulk of Core's
  traffic, so NAT per-GB charges apply mostly to SES/EventBridge/ECR calls.
- **Paid interface endpoints skipped** (~$7/mo each) — not worth it at MVP
  volume; revisit if NAT data processing shows up in Cost Explorer.

## Other cost guards

- **Single region** (us-east-1) — no cross-region replication for MVP.
- **boto3 client singletons** — clients built once; avoids per-call setup cost.
- **S3 lifecycle**: active 30d → archive → expire 90d (or per-file TTL).
- **ECS**: 0.25 vCPU / 512MB Fargate, desired 1, autoscale max 3; Container
  Insights off; log retention 30d.
