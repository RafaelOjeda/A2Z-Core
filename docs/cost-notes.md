# Cost Notes (AWS)

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

## Other cost guards

- **Single region** (us-east-1) — no cross-region replication for MVP.
- **boto3 client singletons** — clients built once; avoids per-call setup cost.
- **S3 lifecycle**: active 30d → archive → expire 90d (or per-file TTL).
