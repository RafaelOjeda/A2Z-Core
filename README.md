# A2Z Core

The shared infrastructure layer every A2Z service depends on — auth, membership,
email, storage, audit, settings, events, and rate limiting. **Not a microservice:**
a set of Python packages (`app/core/`) imported in-process by services inside a
single FastAPI modular monolith on ECS Fargate.

See `CLAUDE.md` (build conventions + gaps) and `A2Z_Core_Design_TestPlan.md`
(authoritative API/schema spec). **For everything else — architecture
diagrams, per-module reference docs, the Omni-Channel service, API/config
reference, testing, CI/CD — see [`docs/README.md`](docs/README.md), the
documentation index.**

## Golden rules

1. Every Core call is in-process (no network hop between services and Core).
2. Every data access is **org-scoped** — no query without an `org_id`.
3. Core never imports from `services/`. Services import from `core/`.
4. Services talk to each other only via **EventBridge events**.
5. Secrets come from IAM task roles, not code/env.
6. Significant actions get an **audit log** entry.

## Local development

Common tasks are in the `Makefile` (`make help` lists them):

```bash
make install          # venv + editable install with dev extras
make test             # whole suite (starts Postgres via docker compose first)
make test-unit        # fast; fully in-process, no backend needed
make lint             # ruff check + format check + mypy --strict (same as CI)
```

### Running tests

The suite runs **AWS against moto and Redis against fakeredis, both
in-process** (`tests/conftest.py`), so the *only* external backend it needs is
**Postgres** — there's no in-process fake for it. `make test` starts a Postgres
container for you; any Postgres reachable at `DATABASE_URL` (default matches
`.env.example` / docker-compose) works equally well. Unit tests need nothing.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

docker compose up -d postgres         # the one backend the suite can't fake
pytest tests/unit -v                  # fast, fully in-process (no backend)
pytest tests/integration -v           # Postgres + in-process moto/fakeredis
pytest tests/load -m load -v          # latency checks (also needs Postgres)
ruff check . && ruff format --check . && mypy app scripts   # lint + format + types

# For manual end-to-end dev against real-ish services (not needed for tests):
make up                               # Postgres + Redis + LocalStack + resources
```

## Build artifacts

```bash
docker build -t a2z-core .            # monolith image (web; worker = same image + cmd override)
bash scripts/build_lambda.sh          # dist/lambda.zip for both out-of-band Lambdas
```

CI (`.github/workflows/ci.yml`) enforces all of the above — lint/format/types,
tests with a 90% coverage gate on `app/core`, the docker build, and
`terraform fmt`/`validate` over `infra/`.

## Layout

```
app/
  config.py            # settings, table registry, rate-limit registry
  core/                # ★ the platform packages (frozen — see docs/core/)
  services/
    omnichannel/       # the first product service built on Core (see docs/services/omnichannel/)
    invoicing/         # Phase 2, v1 built (see app/services/invoicing/CLAUDE.md; roadmap: docs/phase2-invoicing.md)
  routers/             # thin HTTP layer over core/services
  lambdas/             # out-of-band handlers (Cognito, SES/SNS)
infra/                 # Terragrunt (modules + migrations)
scripts/               # local provisioning, Lambda packaging
tests/                 # unit / integration / load
docs/                  # documentation index — see docs/README.md
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
- **Phase F — Invoicing built (2026-07-23)**: v1 implemented per the design
  finalized 2026-07-22 in `app/services/invoicing/CLAUDE.md` (data model, HTTP
  surface at `/v1/invoicing`, state machine, Core dependency map) — 77 tests,
  98% package coverage, `ruff` + `mypy --strict` clean, no Core/Omni-Channel
  regressions (350 tests green repo-wide). Short roadmap in
  `docs/phase2-invoicing.md`. Invoicing imports Core, never the reverse, and
  needed no Core change. Remaining: the `docs/services/invoicing/` reference
  tree (CLAUDE.md §16).
- **Phase G — DoD closure**: CLAUDE.md §15 checklist fully ticked with
  evidence; Core is frozen. Remaining infra deferrals (ACM/HTTPS, Route53,
  RDS) are listed in `infra/README.md` and arrive with deployment/Phase 2.
- **Spec-audit gap closure (G1–G5)**: lazily-created SES config sets now get
  the Bounce/Complaint → SNS event destination CLAUDE.md §8 requires (topic
  ARN via `SES_NOTIFICATIONS_TOPIC_ARN`, wired through the ECS task env), so
  the suppression pipeline works end-to-end in AWS; `send_email` raises the
  spec'd `InvalidAddressError` on malformed recipients; the
  `RateLimitError`-outside-`EmailError` deviation from Design §6 is now
  documented on the class (deliberate — the limiter is generic); all six
  Design §5.4 latency targets have load tests (was three); and
  `create_local_resources.py` creates the sample config set its docstring
  promised.
