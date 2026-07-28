# A2Z Core Deployment Guide

This guide covers deploying A2Z Core to AWS on the single-box shape
described in [`docs/architecture/single-box-mvp.md`](docs/architecture/single-box-mvp.md)
and [`infra/README.md`](infra/README.md) — read those two for the full
rationale; this file is the operator's how-to.

## Architecture

- One EC2 instance running the app + a Postgres container + Caddy (TLS),
  supervised via docker-compose under one systemd unit.
- Managed AWS services: DynamoDB, S3, SES/SNS, EventBridge, SQS, Secrets
  Manager, Cognito, ECR. **No RDS, no ElastiCache, no Lambdas, no ALB.**
- Elastic IP for a static address.
- Auto-restart via systemd (`Restart=always`).

There is no other deployment shape to migrate to at MVP scale — this *is*
the target, not a stepping stone to ECS/EKS. See
[`docs/architecture/microservices-distribution.md`](docs/architecture/microservices-distribution.md)
for the forward-looking (not scheduled) distribution plan if that ever
becomes necessary.

## Prerequisites

- AWS Account with appropriate IAM permissions
- Docker installed locally
- AWS CLI configured
- Terraform + Terragrunt installed

## Deployment steps

### 1. Apply the infrastructure

```bash
cd infra/live/prod
terragrunt run-all plan
terragrunt run-all apply
```

Two inputs need real values before `ec2` (and
`ses-notifications-subscription`) will apply cleanly — both are
deliberately left without safe defaults:

- **`postgres_password`** in `live/prod/ec2/terragrunt.hcl` — pass via
  `-var` or a gitignored `*.auto.tfvars`.
- **A real domain**, DNS-pointed at the `ec2` module's Elastic IP, then set
  in `modules/ec2-simple/user-data.sh`'s `DOMAIN_NAME` and in
  `live/prod/ses-notifications-subscription/terragrunt.hcl`'s
  `endpoint_url`. Until that's done, Caddy serves plain HTTP and the SNS
  subscription sits "pending confirmation" — both inert, not broken.

### 2. Build and push the image

```bash
docker build -t a2z-core:latest .

ECR_URL=$(cd infra/live/prod/ecr && terragrunt output -raw repository_url)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$ECR_URL"
docker tag a2z-core:latest "$ECR_URL:latest"
docker push "$ECR_URL:latest"
```

The box pulls this same image on boot and on every `systemctl restart
a2z-core` (`user-data.sh`'s `ExecStartPre`).

### 3. Access the application

```bash
cd infra/live/prod/ec2 && terragrunt output public_ip
```

- Over HTTP/HTTPS: `http://<PUBLIC_IP>` (or your domain, once DNS + Caddy
  TLS are live).
- Logs: `ssh ubuntu@<PUBLIC_IP>` then `sudo journalctl -u a2z-core -f`
  (captures all three containers — app, postgres, caddy — since
  docker-compose runs in the foreground under this one systemd unit).

## Environment

Set by `user-data.sh` in `/opt/a2z-core/.env` on the box — you don't hand-edit
this file directly; it's rendered from Terraform inputs. The notable ones:

```env
A2Z_ENV=prod
AWS_REGION=us-east-1
DATABASE_URL=postgresql+asyncpg://a2z:<postgres_password>@postgres:5432/a2z
RUN_OMNICHANNEL_WORKER=true
COGNITO_USER_POOL_ID=...
SES_NOTIFICATIONS_TOPIC_ARN=...
```

`RUN_OMNICHANNEL_WORKER=true` is set **only** here — nowhere else does the
SQS-draining loop run, since it's a lifespan background task inside this
same process (`app/services/omnichannel/worker.py::run_forever`), not a
separate container. See
[single-box-mvp.md](docs/architecture/single-box-mvp.md)'s
"Single-process is load-bearing" section before ever changing `--workers`
in the Dockerfile's `CMD` — a second process would silently double every
rate limit and drop realtime updates.

## Backups — read this before going live

`user-data.sh` installs a nightly cron (`backup-postgres.sh`, 03:00) that
`pg_dump`s the box's Postgres container and uploads it to
`s3://<ledger-bucket>/backups/postgres/`. **This has not been
restore-tested.** There is no RDS safety net on this shape — treat a
restore drill as a blocking prerequisite before storing anything you can't
afford to lose, not an optional hardening step.

## Troubleshooting

**Application not starting?**
```bash
ssh ubuntu@<PUBLIC_IP>
sudo systemctl status a2z-core
sudo journalctl -u a2z-core -n 100
docker compose -f /opt/a2z-core/docker-compose.yml ps   # which of the 3 containers is down
```

**ECR authentication failed on the box?** The instance role should handle
this automatically (`ecr:GetAuthorizationToken` + pull permissions,
`infra/modules/iam`) — if it's failing, check the instance profile is
attached, not IAM credentials on the box.

**Database connection issues?**
- Postgres is a container on this same box (`docker compose ps postgres`),
  not a separate managed service — there's no security group or endpoint
  to check, just whether the container is healthy.
- Verify `DATABASE_URL` in `/opt/a2z-core/.env` matches the
  `postgres_password` actually applied via Terraform.

**Webhook / SNS notifications not arriving?**
- Confirm DNS resolves your domain to the box's Elastic IP and Caddy has a
  valid cert (`curl -v https://<domain>/health`).
- Check the SNS subscription's confirmation status in the AWS console — it
  stays "pending" until `endpoint_url` points at a real, reachable HTTPS
  endpoint (see step 1).
