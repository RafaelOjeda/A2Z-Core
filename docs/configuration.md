# Configuration & Environment Variables

> Part of the [documentation index](README.md). Source: [`app/config.py`](../app/config.py), [`.env.example`](../.env.example). See also: [`core/shared-infrastructure.md`](core/shared-infrastructure.md).
> **Authority:** _reference_ — describes current code; if the two disagree, the code wins.

All configuration loads through one `pydantic-settings` object
(`app.config.Settings`, accessed via the cached `settings()` function).
Copy `.env.example` to `.env` for local development.

## Runtime

| Variable | Default | Meaning |
|---|---|---|
| `A2Z_ENV` | `local` | `local` \| `dev` \| `prod`. Gates test-token acceptance (`core.auth`) — HS256 tokens are refused whenever this is `"prod"` |
| `A2Z_LOG_LEVEL` | `INFO` | `DEBUG` only for active debugging — logs are billed per GB (`docs/cost-notes.md`) |
| `AWS_REGION` | `us-east-1` | Region for every boto3 client |
| `AWS_ENDPOINT_URL` | unset | When set, every boto3 client targets this endpoint instead of real AWS (LocalStack). Leave unset in prod |

## Postgres

No Redis anymore — see [single-box-mvp.md](architecture/single-box-mvp.md)
for what replaced it (all in-process: caching, rate limiting, realtime
fan-out).

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://a2z:a2z-local-dev-only@localhost:5432/a2z` | Shared Postgres instance (`omnichannel` + `invoicing` schemas) |

## Omni-Channel worker (single-box MVP)

| Variable | Default | Meaning |
|---|---|---|
| `RUN_OMNICHANNEL_WORKER` | `false` | Starts `worker.run_forever()` as a FastAPI lifespan background task in this same process. Off by default so tests (which boot the app constantly via `TestClient`) never race a live worker against mocked AWS state; the deployed box's `user-data.sh` sets this `true` |
| `OMNICHANNEL_WORKER_POLL_INTERVAL_SECONDS` | `2.0` | Sleep between poll iterations when both SQS queues returned zero messages |

## DynamoDB tables

| Variable | Default |
|---|---|
| `DDB_MEMBERSHIP_TABLE` | `a2z-core-membership` |
| `DDB_AUDIT_TABLE` | `a2z-core-audit` |
| `DDB_SETTINGS_TABLE` | `a2z-core-settings` |
| `DDB_EMAIL_EVENTS_TABLE` | `a2z-core-email-events` |
| `DDB_SUPPRESSION_TABLE` | `a2z-core-suppression` |
| `DDB_FILES_TABLE` | `a2z-core-files` |

## S3 / EventBridge

| Variable | Default | Meaning |
|---|---|---|
| `S3_BUCKET` | `a2z-ledger` | The one private bucket for every service's files |
| `EVENT_BUS_NAME` | `a2z-bus` | The one custom EventBridge bus |

## Omni-Channel SQS

| Variable | Default |
|---|---|
| `OMNICHANNEL_INBOUND_QUEUE` | `a2z-omnichannel-inbound` |
| `OMNICHANNEL_INBOUND_DLQ` | `a2z-omnichannel-inbound-dlq` |
| `OMNICHANNEL_OUTBOUND_QUEUE` | `a2z-omnichannel-outbound` |
| `OMNICHANNEL_OUTBOUND_DLQ` | `a2z-omnichannel-outbound-dlq` |

> **Note**: these four are read by `app/config.py` and used throughout
> `app/services/omnichannel/queues.py` and `aws_resources.py`, but are
> **not yet listed in `.env.example`** — a minor documentation gap in that
> file (defaults apply either way; only worth setting explicitly if you
> need non-default queue names).

## Cognito

| Variable | Default | Meaning |
|---|---|---|
| `COGNITO_USER_POOL_ID` | `""` | Used to build the JWKS URL and `iss` check |
| `COGNITO_REGION` | `us-east-1` | — |
| `COGNITO_APP_CLIENT_ID` | `""` | Not currently checked against the token's `aud` (verification is deliberately audience-agnostic — see [`core/auth.md`](core/auth.md)) |

## SES notifications

| Variable | Default | Meaning |
|---|---|---|
| `SES_NOTIFICATIONS_TOPIC_ARN` | unset | SNS topic each lazily-created SES config set's Bounce/Complaint event destination targets. Unset = bounce/complaint tracking silently skipped (fine for bare local dev) |

## Test tokens

| Variable | Default | Meaning |
|---|---|---|
| `TEST_JWT_SECRET` | `local-development-only-not-a-real-secret` | HS256 signing key for `core.auth.create_test_token`. **Never used when `A2Z_ENV == "prod"`** |

## Registries defined in code, not env vars

These live in `app/config.py` as Python dicts/constants — deliberately
centralized rather than scattered as literals, per `CLAUDE.md` §7/§9/§10:

| Name | Value | Purpose |
|---|---|---|
| `RATE_LIMITS` | `{"email.send": (50, 3600), "ai.parse.user": (30, 60), "ai.parse.org": (500, 86400), "omnichannel.whatsapp.send": (80, 1)}` | `action -> (limit, window_seconds)` — see [`core/rate-limit.md`](core/rate-limit.md) |
| `DDB_BILLING_MODE` | `"PAY_PER_REQUEST"` | Applied to every DynamoDB table — the on-demand cost decision (`docs/cost-notes.md`) |
| `SQS_MAX_RECEIVE_COUNT` | `5` | Shared by the SQS redrive policy and the Omni-Channel worker's give-up threshold — see [message flow](services/omnichannel/message-flow.md) |

## AWS credentials

Never set `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` in a real
environment — production credentials come from the EC2 instance's IAM role
(golden rule #5). The `.env.example` dummy values (`test`/`test`,
`testing`/`testing` in `tests/conftest.py`) exist only to satisfy boto3's
requirement for *some* credential value when talking to LocalStack/moto,
which don't perform real credential checks.
