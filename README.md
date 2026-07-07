# A2Z Core

The shared infrastructure layer every A2Z service depends on — auth, membership,
email, storage, audit, settings, events, and rate limiting. **Not a microservice:**
a set of Python packages (`app/core/`) imported in-process by services inside a
single FastAPI modular monolith on ECS Fargate.

See `CLAUDE.md` (build conventions + gaps) and `A2Z_Core_Design_TestPlan.md`
(authoritative API/schema spec).

## Golden rules

1. Every Core call is in-process (no network hop between services and Core).
2. Every data access is **org-scoped** — no query without an `org_id`.
3. Core never imports from `services/`. Services import from `core/`.
4. Services talk to each other only via **EventBridge events**.
5. Secrets come from IAM task roles, not code/env.
6. Significant actions get an **audit log** entry.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Optional: real backing services (otherwise tests use moto + fakeredis)
docker compose up -d
cp .env.example .env
python -m scripts.create_local_resources    # tables, bucket, bus, SES config

pytest tests/unit -v                  # fast, in-process AWS mocks
pytest tests/integration -v           # moto/LocalStack-backed
pytest tests/load -m load -v          # latency checks
ruff check . && mypy app              # lint + types
```

## Layout

```
app/
  config.py            # settings, table registry, rate-limit registry
  core/                # ★ the platform packages (build first)
  services/            # stubs until Phase 2+
  routers/             # thin HTTP layer over core
  lambdas/             # out-of-band handlers (Cognito, SES/SNS)
infra/                 # Terragrunt (modules + migrations)
scripts/               # local provisioning, migrations
tests/                 # unit / integration / load
docs/                  # events, retention, cost notes
```

## Status

Phase 1 (Core) complete: all 8 modules (`auth`, `audit`, `membership`,
`settings`, `rate_limit`, `events`, `storage`, `email`), both Lambdas, the HTTP
layer, and Terragrunt data-plane infra are implemented and tested — 74 tests
green, `core/` coverage 93%, `ruff` + `mypy --strict` clean, load-test latencies
within Design §5.4 targets. See `CLAUDE.md §13` for the build order and §15 for
the Definition of Done.

Gap-closure progress (verified evidence per phase):

- **Phase A — verification pass**: reproduced on 2026-07-07 under Python
  3.12.3 — 74 tests green (70 unit/integration + 4 load), `app/core` coverage
  93%, `ruff check` and `mypy --strict` clean.
- **Phase B — Python 3.12 alignment**: `requires-python >=3.12`, ruff
  `target-version py312`, mypy `python_version 3.12`, `.python-version` added;
  full suite re-verified green under 3.12.
- **Phase C — Dockerfile + Lambda packaging**: multi-stage `python:3.12-slim`
  image (non-root, `/health` HEALTHCHECK; worker = same image with an ECS
  command override), `.dockerignore`, and `scripts/build_lambda.sh` producing
  `dist/lambda.zip` (both handlers + deps, boto3 excluded — Lambda runtime
  provides it). Fixed setuptools packaging that silently dropped `app.*`
  subpackages from non-editable installs. Verified: wheel install boots
  uvicorn and serves `/health`; zip contents inspected. Image build itself is
  verified by the CI docker job (base-image pulls are blocked in the dev
  sandbox's network policy).
- **Phase D — CI**: `.github/workflows/ci.yml` — lint+format+types, tests
  with a 90% coverage gate on `app/core` (load tests advisory), docker image
  build, and terraform fmt/validate over `infra/modules`. Python 3.12, no
  service containers (suite is in-process moto + fakeredis).
- **Phase E — control-plane Terragrunt**: vpc (2-AZ, single NAT, free DDB/S3
  gateway endpoints, alb→app→redis SG chain), iam (least-privilege task +
  execution + two Lambda roles matching `app/config.py` resource names), redis
  (ElastiCache t4g.micro), cognito (user pool + SPA client + both Lambdas from
  `dist/lambda.zip`, post-confirm trigger + SNS wiring), ecs (ECR, Fargate task
  + ALB with `/health` matcher 200, CPU autoscaling 1→3) — plus live/prod
  compositions with `dependency` wiring and the previously missing `ses` live
  composition. `terraform fmt` clean; `terraform validate` runs in the CI infra
  job (provider downloads are policy-blocked in the dev sandbox).
- **Phase F — Invoicing kickoff**: roadmap in `docs/phase2-invoicing.md`
  (outline only — Invoicing is a service that imports Core, never the
  reverse; no invoicing code lands until Core is frozen).
