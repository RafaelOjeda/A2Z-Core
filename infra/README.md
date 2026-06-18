# Infrastructure (Terragrunt)

Terraform/Terragrunt for A2Z Core's AWS resources. The data-plane modules here
mirror exactly what `scripts/create_local_resources.py` stands up against
LocalStack (CLAUDE.md §12), so local and AWS stay in sync.

## Layout

```
infra/
├── terragrunt.hcl            # root: remote state, provider, default tags
├── modules/
│   ├── dynamodb/             # all 6 Core tables (on-demand, GSIs, TTL, PITR)
│   ├── s3/                   # private ledger bucket (lifecycle, SSE, no public)
│   ├── eventbridge/          # a2z-bus custom event bus
│   └── ses/                  # SNS notifications topic + policy + domain identity
└── live/
    └── prod/                 # per-module Terragrunt compositions
        ├── dynamodb/
        ├── s3/
        └── eventbridge/
```

## Apply

```bash
cd infra/live/prod
terragrunt run-all plan
terragrunt run-all apply
```

## Cost posture (CLAUDE.md §10/§11)

- **DynamoDB on-demand** everywhere — no capacity planning at MVP volume.
- **TTL enabled** on audit / email-events / files — DynamoDB expires items free.
- **S3 lifecycle**: 30d → IA → Glacier, expire 90d; abort stale multipart uploads.
- **EventBridge** ~$1/M events — negligible.
- **Single region** (us-east-1).

## Not yet codified (planned)

These appear in the target layout but are deliberately out of the Phase 1 Core
data-plane scope. Each is a focused follow-up module:

- **cognito** — User Pool + app client + Post-Confirmation trigger wiring to
  `app/lambdas/cognito_post_confirm.py`.
- **redis** — ElastiCache (t4g.micro) for the JWKS/settings/rate-limit caches.
- **ecs** — Fargate service (web + worker), ALB, autoscaling.
- **vpc** — subnets, security groups, VPC endpoints.
- **iam** — task roles (DynamoDB/S3/SES/EventBridge least-privilege), no static keys.

Config sets are intentionally **not** in Terraform: Core creates one per
`{org_id}-{service_type}` lazily on first send (CLAUDE.md §8). Terraform owns the
shared SNS topic the `ses_notifications` Lambda subscribes to.
