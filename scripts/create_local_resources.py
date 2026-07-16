"""Provision Core's AWS resources against LocalStack for local dev / tests.

Mirrors what Terragrunt creates in AWS (tables + GSIs, the S3 bucket, EventBridge
bus, a sample SES config set) so integration tests have something to hit
(CLAUDE.md §12). Idempotent: re-running skips resources that already exist.

    python -m scripts.create_local_resources
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_sqs.literals import QueueAttributeNameType

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


def _create_queue(name: str, *, redrive_target_arn: str | None = None) -> str:
    """Create (or reuse) one SQS queue; returns its ARN.

    ``redrive_target_arn`` wires a redrive policy to a DLQ (maxReceiveCount=5,
    Omni-Channel CLAUDE.md §5.6: "bounded retry with backoff, then DLQ +
    alarm" — the alarm side is infra/Step 8, this is the queue-level plumbing).
    """
    sqs = clients.sqs()
    attributes: dict[QueueAttributeNameType, str] = {}
    if redrive_target_arn:
        attributes["RedrivePolicy"] = json.dumps(
            {"deadLetterTargetArn": redrive_target_arn, "maxReceiveCount": 5}
        )
    try:
        resp = sqs.create_queue(QueueName=name, Attributes=attributes)
        log.info("sqs.queue.created", extra={"queue": name})
    except ClientError as exc:
        if _exists(exc, "QueueAlreadyExists"):
            resp = sqs.get_queue_url(QueueName=name)
            log.info("sqs.queue.exists", extra={"queue": name})
        else:
            raise
    queue_url = resp["QueueUrl"]
    arn_resp = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
    return str(arn_resp["Attributes"]["QueueArn"])


def create_omnichannel_queues() -> None:
    """Create Omni-Channel's shared inbound/outbound queues + their DLQs (§5.6, §12).

    One inbound queue for every channel (§5.2 extensibility invariant) and
    one outbound queue -- never per-channel queues. DLQs are created first so
    the main queues' redrive policies can reference their ARNs.
    """
    s = settings()
    inbound_dlq_arn = _create_queue(s.omnichannel_inbound_dlq)
    outbound_dlq_arn = _create_queue(s.omnichannel_outbound_dlq)
    _create_queue(s.omnichannel_inbound_queue, redrive_target_arn=inbound_dlq_arn)
    _create_queue(s.omnichannel_outbound_queue, redrive_target_arn=outbound_dlq_arn)


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
    create_omnichannel_queues()
    verify_ses_identity()
    log.info("provision.complete", extra={"endpoint": settings().aws_endpoint_url})


if __name__ == "__main__":
    main()


# Convenience re-export so tests can provision without spawning a subprocess.
def provision_all() -> dict[str, Any]:
    main()
    return {"tables": list(settings().tables.values()), "bucket": s3_bucket_name()}
