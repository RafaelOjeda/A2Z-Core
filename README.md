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

Phase 0 (scaffolding) in progress. Build order: see `CLAUDE.md §13`.
