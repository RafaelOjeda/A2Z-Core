# Infrastructure (Terragrunt)

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
│   ├── redis/                # ElastiCache cache.t4g.micro (single node)
│   ├── cognito/              # user pool, SPA client, both Lambdas + trigger/SNS wiring
│   └── ecs/                  # ECR, cluster, task def, ALB (:80), service, autoscaling
└── live/
    └── prod/                 # per-module Terragrunt compositions
        ├── dynamodb/  s3/  eventbridge/  ses/
        └── vpc/  iam/  redis/  cognito/  ecs/
```

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
- **RDS Postgres** — arrives with the Invoicing service (Phase 2, Design §3.2).

Config sets are intentionally **not** in Terraform: Core creates one per
`{org_id}-{service_type}` lazily on first send (CLAUDE.md §8). Terraform owns the
shared SNS topic the `ses_notifications` Lambda subscribes to — the cognito
module subscribes the Lambda to it.
