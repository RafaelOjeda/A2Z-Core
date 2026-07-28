# Infrastructure (Terragrunt)

> See also: [`docs/architecture/deployment.md`](../docs/architecture/deployment.md) and [`docs/architecture/single-box-mvp.md`](../docs/architecture/single-box-mvp.md) for the deployment shape this repo now targets.

Terraform/Terragrunt for A2Z Core's AWS resources. The data-plane modules here
mirror exactly what `scripts/create_local_resources.py` stands up against
LocalStack (CLAUDE.md §12), so local and AWS stay in sync. Everything else
carries the **single-box MVP**: one EC2 instance running the app + a Postgres
container + Caddy (TLS), one IAM role, one VPC with a public subnet and no
NAT gateway. This replaced an earlier ECS Fargate + ALB + ElastiCache + RDS
shape that was sized for ~1K orgs; at one user, that shape cost roughly
6x what this one does for identical functionality. See the cost table below.

## Layout

```
infra/
├── terragrunt.hcl                     # root: remote state, provider, default tags
├── modules/
│   ├── dynamodb/                      # all 6 Core tables (on-demand, GSIs, TTL, PITR)
│   ├── s3/                            # private ledger bucket (lifecycle, SSE, no public)
│   ├── eventbridge/                   # a2z-bus custom event bus (publisher only; no rules/subscribers yet)
│   ├── ses/                           # SNS notifications topic + policy + domain identity
│   ├── ecr/                           # the one image repo (app/Dockerfile)
│   ├── vpc/                           # one public subnet, one AZ, free DDB/S3 gateway endpoints, one SG
│   ├── iam/                           # one EC2 instance role -- Dynamo/S3/SES/EventBridge/SQS/SecretsManager/ECR-pull
│   ├── cognito/                       # user pool + SPA client only (no Lambda -- see app/dependencies.py)
│   ├── ec2-simple/                    # the instance: app + postgres + caddy via docker-compose (user-data.sh)
│   ├── ses-notifications-subscription/ # SNS -> HTTPS subscription to the box's webhook route
│   └── sqs-omnichannel/               # Omni-Channel's inbound/outbound queues + DLQs
└── live/
    └── prod/                          # per-module Terragrunt compositions, one dir per module above
```

Cross-module wiring uses Terragrunt `dependency` blocks with `mock_outputs`
(so `validate`/`plan` work before first apply). Apply order, derived from
those dependencies:

```
dynamodb, s3, eventbridge, ses, ecr, sqs-omnichannel   (leaves, no deps)
  -> vpc
  -> iam            (needs eventbridge + ecr)
  -> cognito         (leaf again -- no Lambda, no deps left)
  -> ec2             (needs vpc, iam, ecr, ses, cognito)
  -> ses-notifications-subscription   (needs ses + ec2; see its own header for why it's last)
```

## Apply

```bash
cd infra/live/prod
terragrunt run-all plan
terragrunt run-all apply

# Build and push the image once ecr exists (the ec2 module pulls it on boot):
docker build -t <ecr_repository_url>:latest .
docker push <ecr_repository_url>:latest
```

**Two things you must set before `ec2` applies cleanly (deliberately no
defaults, to avoid a real value ever being committed):**

- `postgres_password` in `live/prod/ec2/terragrunt.hcl` -- override via
  `-var` or a gitignored `*.auto.tfvars`.
- A real domain pointed at the `ec2` module's Elastic IP, then
  `endpoint_url` in `live/prod/ses-notifications-subscription/terragrunt.hcl`
  and the (currently blank) `DOMAIN_NAME` in
  `modules/ec2-simple/user-data.sh` -- see that file's Caddyfile comment.
  Until DNS is live, Caddy serves plain HTTP and the SNS subscription sits
  "pending confirmation"; both are inert, not broken, in that state.

## Cost posture

At one user, ~$22/mo, down from ~$130-140/mo for the prior ECS/RDS/
ElastiCache/ALB/NAT shape:

| Item | ~$/mo | Notes |
|---|---|---|
| EC2 (t4g.small) | ~12 | app + Postgres + Caddy, one box |
| EBS gp3 30GB | ~2.40 | Postgres data lives here -- see the backup note below |
| Elastic IP | ~3.60 | |
| DynamoDB (on-demand) | ~1 | all 6 Core tables |
| S3 | ~1 | invoice PDFs, message attachments |
| SES / SNS / SQS / EventBridge | ~0-1 | usage-based, negligible at this volume |
| Secrets Manager | ~0.40/secret | per-org channel credentials |

Cut entirely: NAT gateway (~$32), ALB (~$18), ECS/Fargate + autoscaling
(~$30), ElastiCache (~$13), RDS (~$20), both Lambdas. See
[`docs/architecture/single-box-mvp.md`](../docs/architecture/single-box-mvp.md)
for what replaced each one in application code.

**Non-negotiable operational gap:** the nightly `pg_dump -> S3` cron in
`user-data.sh` is the only backup for Postgres -- there is no RDS safety
net. A restore from it has not been drilled; treat that as the single
highest-risk item before trusting this with real data (matches the same
open item already tracked in `app/services/omnichannel/CLAUDE.md` §16).

## Not yet codified (deliberately out of scope for the single-box MVP)

- **A CloudWatch agent / alarms** -- `metrics.py` writes structured logs
  only; there is no agent shipping them off-box and no alarms watching the
  series it emits. Revisit if/when this needs to distribute again.
- **Route53** -- DNS for the box's domain is a manual step (see above), not
  Terraform-managed.
- **A real HA story** -- one instance, one AZ, one Postgres. Documented
  trade-off of the single-box MVP, not an oversight.

Config sets are intentionally **not** in Terraform: Core creates one per
`{org_id}-{service_type}` lazily on first send (CLAUDE.md §8), cached
in-process. Terraform owns the shared SNS topic; the
`ses-notifications-subscription` module subscribes the app's HTTPS route to
it (`app/routers/ses_notifications.py` -- no Lambda anymore).
