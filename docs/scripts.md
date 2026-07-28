# Scripts & Build Tooling

> Part of the [documentation index](README.md). See also: [deployment architecture](architecture/deployment.md), [`infra/README.md`](../infra/README.md).
> **Authority:** _reference_ — describes current code; if the two disagree, the code wins.

## `scripts/create_local_resources.py`

Provisions every AWS resource Core and Omni-Channel need, against
LocalStack for manual local dev **or** moto for automated tests — the
exact same code path, so the two never drift.

```bash
python -m scripts.create_local_resources
```

What it creates, in order (`main()`):

1. **DynamoDB tables** — from `app.aws_resources.table_definitions()`
   (the same specs Terragrunt's `dynamodb` module encodes), enabling TTL on
   `audit`/`email_events`/`files`.
2. **S3 bucket** (`a2z-ledger`).
3. **EventBridge bus** (`a2z-bus`).
4. **Omni-Channel's SQS queues** (`app.services.omnichannel.aws_resources.create_queues`)
   — inbound/outbound + their DLQs, DLQs created first so the main queues'
   redrive policies can reference their ARNs.
5. **A sample SES domain identity** (`example.com`) so local sends succeed.
6. **A sample SES configuration set** (`local-dev-invoicing`), including
   the Bounce/Complaint → SNS event destination if
   `SES_NOTIFICATIONS_TOPIC_ARN` is set.

Every step is idempotent — re-running skips resources that already exist
(`ResourceInUseException`/`BucketAlreadyOwnedByYou`/`QueueAlreadyExists`/etc.
are all caught and logged as `.exists` rather than raised). `tests/conftest.py`'s
`aws` fixture calls this same `main()` inside a `moto.mock_aws()` context for
every test that needs AWS resources — see [testing](testing.md).

## `scripts/check_docs.py`

The documentation integrity gate — stdlib-only, no project install needed:

```bash
python -m scripts.check_docs
```

Two checks, both aimed at doc drift:

1. **Broken relative links** — every `[text](path)` in a tracked Markdown
   file (excluding `.venv`, caches, etc.) must resolve to a real file.
   External URLs (`http(s)://`, `mailto:`) and pure `#anchor` links are
   skipped; this validates paths, not the web or heading anchors.
2. **INDEX.md registration** — every Markdown file under `docs/` (except
   `README.md` and `INDEX.md` themselves) must be linked from
   [`docs/INDEX.md`](INDEX.md), so a new doc can't be added and silently
   orphaned.

Exits non-zero and prints each problem on failure. Runs as the `docs` job
in [CI](ci-cd.md#docs); run it locally after adding, moving, or renaming any
doc.

## No Lambda packaging script anymore

`scripts/build_lambda.sh` and `app/lambdas/` are deleted. Both former
out-of-band handlers moved in-process (single-box MVP —
[single-box-mvp.md](architecture/single-box-mvp.md)):
`cognito_post_confirm` → `app/dependencies.py::current_user`;
`ses_notifications` → `app/routers/ses_notifications.py`. Neither needs a
separate build/package step — they ship as part of the one application
image.

## `infra/migrations/`

Dated, idempotent DynamoDB backfill scripts — see
[migration strategy](migrations.md#dynamodb-core--rules-not-tooling) for
the rules and the one example script's shape.

## Docker

```bash
docker build -t a2z-core .            # the one monolith image -- app + Omni-Channel worker
                                       # (lifespan task, RUN_OMNICHANNEL_WORKER), one process
docker compose up -d                  # LocalStack + Postgres for local dev/tests -- no Redis
```

See [deployment architecture](architecture/deployment.md) for the
single-box deployment shape and what runs where.
