# Infrastructure (Terragrunt)

> See also: [`docs/architecture/deployment.md`](../docs/architecture/deployment.md) for the two deployment shapes this repo describes (Core's ECS Fargate control plane vs. Omni-Channel's single-EC2 MVP) and exactly which of the modules below back which shape.

Terraform/Terragrunt for A2Z Core's AWS resources. The data-plane modules here
mirror exactly what `scripts/create_local_resources.py` stands up against
LocalStack (CLAUDE.md §12), so local and AWS stay in sync. The control-plane
modules (vpc/iam/redis/cognito/ecs) carry the monolith itself: Fargate behind
an ALB, ElastiCache for the Core caches, Cognito with the post-confirm trigger,
and least-privilege task/Lambda roles (golden rule #5: no static keys).

## Layout

```
infra/
├── terragrunt.hcl            # root: remote state, provider, default tags
├── modules/
│   ├── dynamodb/             # all 6 Core tables (on-demand, GSIs, TTL, PITR)
│   ├── s3/                   # private ledger bucket (lifecycle, SSE, no public)
│   ├── eventbridge/          # a2z-bus custom event bus
│   ├── ses/                  # SNS notifications topic + policy + domain identity
│   ├── vpc/                  # 2-AZ subnets, 1 NAT, free DDB/S3 gateway endpoints, SGs
│   ├── iam/                  # task + execution + Lambda roles (least privilege)
│   ├── redis/                # ElastiCache cache.t4g.micro (single node) -- Core's
│   │                         #   control-plane cache; Omni-Channel's MVP EC2 box uses
│   │                         #   an on-box Redis container instead (not this module)
│   ├── cognito/              # user pool, SPA client, both Lambdas + trigger/SNS wiring
│   ├── ecs/                  # ECR, cluster, task def, ALB (:80), service, autoscaling
│   ├── rds/                  # single-AZ Postgres (db.t4g.micro) -- codified ahead of
│   │                         #   need, see the drift note below
│   └── sqs-omnichannel/      # Omni-Channel's inbound/outbound queues + DLQs
└── live/
    └── prod/                 # per-module Terragrunt compositions
        ├── dynamodb/  s3/  eventbridge/  ses/
        ├── vpc/  iam/  redis/  cognito/  ecs/
        └── rds/  sqs-omnichannel/
```

> **Drift note**: `modules/rds` + `live/prod/rds` are codified but nothing
> currently deploys against them — Omni-Channel's Postgres runs as an
> on-box/docker-compose container today
> (`app/services/omnichannel/CLAUDE.md` §12 explicitly defers RDS to a
> future "distribution phase"), and `docs/phase2-invoicing.md` still lists
> an RDS module as Phase 2 future work. Treat this module as pre-built
> infrastructure for whichever need arrives first, not as evidence either
> phase has started. See
> [Omni-Channel known issues](../docs/services/omnichannel/known-issues.md#4-rds-terraform-module-exists-ahead-of-both-phases-that-would-use-it).
>
> Not yet codified at all: `ec2-app`, `secretsmanager-channels`, and
> `ses-receipt-rules` — the three modules Omni-Channel's single-EC2 MVP
> (`app/services/omnichannel/CLAUDE.md` §12) still needs.

Cross-module wiring uses Terragrunt `dependency` blocks with `mock_outputs`
(so `validate`/`plan` work before first apply); Terragrunt derives the apply
order from them: data-plane + vpc → iam → redis/cognito → ecs.

## Apply

```bash
# 1. The cognito module deploys app/lambdas/* from one zip — build it first:
bash scripts/build_lambda.sh                      # -> dist/lambda.zip

cd infra/live/prod
terragrunt run-all plan
terragrunt run-all apply

# 2. ECS pulls the monolith image from the ECR repo the ecs module creates —
#    build and push before (or right after) the first ecs apply:
docker build -t <ecr_repository_url>:latest . && docker push <ecr_repository_url>:latest
```

## Cost posture (CLAUDE.md §10/§11)

- **DynamoDB on-demand** everywhere — no capacity planning at MVP volume.
- **TTL enabled** on audit / email-events / files — DynamoDB expires items free.
- **S3 lifecycle**: 30d → IA → Glacier, expire 90d; abort stale multipart uploads.
- **EventBridge** ~$1/M events — negligible.
- **Single region** (us-east-1).

## Not yet codified (planned)

- **ACM + HTTPS listener** — the ALB serves :80 only until a domain + cert
  exist; add the :443 listener and redirect then.
- **Route53** — DNS for the ALB and the SES domain-verification records.
- **RDS actually wired up and applied** — the `rds` module itself already
  exists (see the drift note above); what's still pending is a real
  `terragrunt apply` and something pointing `DATABASE_URL` at it instead of
  a container.

Config sets are intentionally **not** in Terraform: Core creates one per
`{org_id}-{service_type}` lazily on first send (CLAUDE.md §8). Terraform owns the
shared SNS topic the `ses_notifications` Lambda subscribes to — the cognito
module subscribes the Lambda to it.
