# A2Z Core — common dev tasks. `make help` lists them.
#
# The test suite runs AWS against moto and Redis against fakeredis, both
# in-process (tests/conftest.py) — so the *only* external backend it needs is
# Postgres, which has no in-process fake. The `pg-up` prerequisite on the test
# targets starts it; any Postgres reachable at DATABASE_URL (default:
# postgresql+asyncpg://a2z:a2z-local-dev-only@localhost:5432/a2z, matching
# docker-compose and .env.example) works just as well.

.PHONY: help install pg-up pg-down up down resources test test-unit \
        test-integration test-load lint fmt

VENV   ?= .venv
BIN    := $(VENV)/bin
PYTEST := $(BIN)/pytest

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install the package with dev extras
	python3 -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"

pg-up: ## Start just the Postgres container (the one backend tests can't fake)
	docker compose up -d postgres

pg-down: ## Stop the Postgres container
	docker compose stop postgres

up: ## Start all local backing services (Postgres, Redis, LocalStack) for manual dev
	docker compose up -d
	cp -n .env.example .env || true
	$(BIN)/python -m scripts.create_local_resources

down: ## Stop all local backing services
	docker compose down

test: pg-up ## Run the whole suite (unit + integration + load) against Postgres
	$(PYTEST)

test-unit: ## Fast unit tests — fully in-process (moto + fakeredis), no backend needed
	$(PYTEST) tests/unit

test-integration: pg-up ## Integration tests (Postgres + in-process moto/fakeredis)
	$(PYTEST) tests/integration

test-load: pg-up ## Load / latency checks
	$(PYTEST) tests/load -m load

lint: ## ruff lint + format check + mypy --strict (same as CI)
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy app scripts

fmt: ## Auto-format and auto-fix with ruff
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .
