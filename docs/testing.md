# Testing

> Part of the [documentation index](README.md). See also: [CI/CD](ci-cd.md).
> **Authority:** _reference_ — describes current code; if the two disagree, the code wins.

## Test layout

```
tests/
├── conftest.py                    # shared fixtures: aws (moto), _reset_core_state, make_token
├── unit/                          # fast, fully mocked
│   ├── test_auth.py, test_auth_cognito.py, test_events.py,
│   │   test_rate_limit.py, test_realtime.py, test_secrets.py
│   └── omnichannel/               # adapter/registry/metrics unit tests
├── integration/                   # moto-backed (+ real Postgres for omnichannel/invoicing)
│   ├── test_api.py, test_audit.py, test_email.py, test_dependencies.py,
│   │   test_ses_notifications_router.py, test_membership.py, test_migration.py,
│   │   test_provisioning.py, test_realtime.py, test_secrets.py,
│   │   test_settings.py, test_storage.py
│   └── omnichannel/                # connections, dlq, inbox, media, message_flow,
│                                    # models, routing, stream, worker_loop
└── load/                          # latency assertions (pytest -m load)
    ├── test_load.py
    └── omnichannel/test_load.py
```

## How the suite runs without any real AWS

Core's tests run entirely **in-process** against **moto** (AWS mocks) — no
Docker, no LocalStack required in CI (`tests/conftest.py`). There is no
Redis to mock anymore (single-box MVP,
[architecture/single-box-mvp.md](architecture/single-box-mvp.md)) — an
autouse `_reset_core_state` fixture clears the in-process realtime broker,
rate-limit windows, and TTL caches between tests instead:

```python
@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        clients.reset_clients()
        provision()  # scripts.create_local_resources.main()
        yield
    clients.reset_clients()

@pytest.fixture(autouse=True)
def _reset_core_state() -> None:
    from app.core import cache, rate_limit, realtime
    realtime.reset()
    rate_limit.reset()
    cache.clear_all()
```

- The `aws` fixture provisions every Core (and Omni-Channel SQS) resource
  inside a `moto.mock_aws()` context using the **same**
  `scripts/create_local_resources.py` that provisions LocalStack for manual
  dev — so tests and local dev never drift on table/GSI shape.
- `_reset_core_state` is **autouse** — since these modules hold plain
  module-global state rather than a per-test-isolated backend, without this
  a rate-limit window or cached secret set in one test would leak into the
  next.
- `make_token` mints valid HS256 test JWTs via `core.auth.create_test_token`
  — no real Cognito pool needed, ever, in tests.

**The one exception**: Omni-Channel's Postgres data layer has no in-process
emulator equivalent to moto, so `tests/integration/omnichannel/*` requires a
real reachable Postgres (`DATABASE_URL`) — the `postgres` service container
in `docker-compose.yml` locally, or the `postgres` service container CI
spins up (`.github/workflows/ci.yml`). See
`tests/integration/omnichannel/conftest.py` for how the schema is rebuilt
per session via `Base.metadata.create_all()` (not by running Alembic
migrations — see [`docs/migrations.md`](migrations.md) for why that
matters) and truncated between tests.

## Running the suite

```bash
pytest tests/unit -v                          # fast, mocked
pytest tests/integration -v                   # moto + real Postgres for omnichannel/invoicing
pytest -m "not load" --cov=app/core --cov=app/services/omnichannel --cov=app/services/invoicing --cov-report=term-missing
pytest tests/load -m load -v                  # latency checks (jittery on shared runners)
```

Pytest markers (`pyproject.toml`): `load` (deselect with `-m "not load"`),
`integration`, `postgres` (deselect with `-m "not postgres"` if no
Postgres is reachable locally).

## Coverage gates

Three **independent** 90% gates, all computed from one coverage run, so a
dip in one package can never hide behind the others:

```bash
coverage report --include="app/core/*" --fail-under=90
coverage report --include="app/services/omnichannel/*" --fail-under=90
coverage report --include="app/services/invoicing/*" --fail-under=90
```

## Cross-org isolation tests

Every Core module and every Omni-Channel Postgres table has at least one
test proving cross-org access fails — this is a hard requirement, not a
nice-to-have (`CLAUDE.md` §4; `app/services/omnichannel/CLAUDE.md` §16).
See each module's reference doc under [`docs/core/`](core/README.md) and
[`docs/services/omnichannel/`](services/omnichannel/README.md) for where
that guarantee is structurally enforced (the test proves it; the code in
`_assert_org_scope`-style checks and key design is *why* it holds).

## Load tests

`tests/load/` asserts the latency targets stated in
`A2Z_Core_Design_TestPlan.md` §5.4 and each module's own docstring (e.g.
`get_membership` p99 < 50ms, `send_email` < 500ms, `log_audit` < 50ms).
CI runs these with `continue-on-error: true` — they're advisory, since
absolute latency numbers are jittery on shared GitHub-hosted runners.

## Writing a new test

- Reach for the `aws` fixture for anything touching Core; the autouse
  `_reset_core_state` fixture already handles in-process state for you.
- Use `make_token`/`core.auth.create_test_token` for an authenticated
  request — never construct a real Cognito flow in a test.
- For Omni-Channel Postgres tests, use the `session` fixture from
  `tests/integration/omnichannel/conftest.py`.
- Add a cross-org isolation test for any new org-scoped query.
