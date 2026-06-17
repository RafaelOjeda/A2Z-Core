"""Provision Core's AWS resources against LocalStack for local dev / tests.

Mirrors what Terragrunt creates in AWS (tables + GSIs, the S3 bucket, EventBridge
bus, a sample SES config set) so integration tests have something to hit
(CLAUDE.md §12). Idempotent: re-running skips resources that already exist.

    python -m scripts.create_local_resources
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from app.aws_resources import (
    TABLES_WITH_TTL,
    TTL_ATTRIBUTE,
    event_bus_name,
    s3_bucket_name,
    table_definitions,
)
from app.config import settings
from app.core import clients
from app.core.logging import get_logger

log = get_logger("scripts.provision")


def _exists(error: ClientError, *codes: str) -> bool:
    return error.response.get("Error", {}).get("Code", "") in codes


def create_tables() -> None:
    ddb = clients.dynamodb()
    table_names = settings().tables
    for logical, spec in table_definitions().items():
        name = table_names[logical]
        try:
            ddb.create_table(TableName=name, **spec)
            ddb.get_waiter("table_exists").wait(TableName=name)
            log.info("ddb.table.created", extra={"table": name})
        except ClientError as exc:
            if _exists(exc, "ResourceInUseException"):
                log.info("ddb.table.exists", extra={"table": name})
            else:
                raise
        if logical in TABLES_WITH_TTL:
            _enable_ttl(name)


def _enable_ttl(table: str) -> None:
    ddb = clients.dynamodb()
    try:
        ddb.update_time_to_live(
            TableName=table,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": TTL_ATTRIBUTE},
        )
    except ClientError as exc:
        # LocalStack/AWS rejects re-enabling already-enabled TTL; that's fine.
        if not _exists(exc, "ValidationException"):
            raise


def create_bucket() -> None:
    s3 = clients.s3()
    bucket = s3_bucket_name()
    try:
        s3.create_bucket(Bucket=bucket)
        log.info("s3.bucket.created", extra={"bucket": bucket})
    except ClientError as exc:
        if _exists(exc, "BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            log.info("s3.bucket.exists", extra={"bucket": bucket})
        else:
            raise


def create_event_bus() -> None:
    eb = clients.eventbridge()
    bus = event_bus_name()
    try:
        eb.create_event_bus(Name=bus)
        log.info("events.bus.created", extra={"bus": bus})
    except ClientError as exc:
        if _exists(exc, "ResourceAlreadyExistsException"):
            log.info("events.bus.exists", extra={"bus": bus})
        else:
            raise


def verify_ses_identity(domain: str = "example.com") -> None:
    """Register a sample SES domain identity so local sends succeed."""
    ses = clients.ses()
    try:
        ses.verify_domain_identity(Domain=domain)
        log.info("ses.identity.verified", extra={"domain": domain})
    except ClientError:
        # SES emulation varies; non-fatal for resource bootstrap.
        log.info("ses.identity.skipped", extra={"domain": domain})


def main() -> None:
    create_tables()
    create_bucket()
    create_event_bus()
    verify_ses_identity()
    log.info("provision.complete", extra={"endpoint": settings().aws_endpoint_url})


if __name__ == "__main__":
    main()


# Convenience re-export so tests can provision without spawning a subprocess.
def provision_all() -> dict[str, Any]:
    main()
    return {"tables": list(settings().tables.values()), "bucket": s3_bucket_name()}
