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
from app.config import SQS_MAX_RECEIVE_COUNT, settings
from app.core import clients
from app.core.logging import get_logger
from app.services.omnichannel.aws_resources import create_queues as create_omnichannel_queues

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

    ``redrive_target_arn`` wires a redrive policy to a DLQ (Omni-Channel
    CLAUDE.md §5.6: "bounded retry with backoff, then DLQ + alarm"). The
    threshold comes from ``config.SQS_MAX_RECEIVE_COUNT`` so it can't drift
    from the worker's give-up threshold.
    """
    sqs = clients.sqs()
    attributes: dict[QueueAttributeNameType, str] = {}
    if redrive_target_arn:
        attributes["RedrivePolicy"] = json.dumps({
            "deadLetterTargetArn": redrive_target_arn,
            "maxReceiveCount": SQS_MAX_RECEIVE_COUNT,
        })
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


def verify_ses_identity(domain: str = "example.com") -> None:
    """Register a sample SES domain identity so local sends succeed."""
    ses = clients.ses()
    try:
        ses.verify_domain_identity(Domain=domain)
        log.info("ses.identity.verified", extra={"domain": domain})
    except ClientError:
        # SES emulation varies; non-fatal for resource bootstrap.
        log.info("ses.identity.skipped", extra={"domain": domain})


def create_sample_config_set(name: str = "local-dev-invoicing") -> None:
    """Create the sample SES config set CLAUDE.md §12 promises.

    Mirrors what Core builds lazily per {org_id}-{service_type} on first send,
    including the SNS event destination when SES_NOTIFICATIONS_TOPIC_ARN is set.
    """
    ses = clients.ses()
    try:
        ses.create_configuration_set(ConfigurationSet={"Name": name})
        log.info("ses.configset.created", extra={"config_set": name})
    except ClientError as exc:
        if _exists(exc, "ConfigurationSetAlreadyExists", "AlreadyExists"):
            log.info("ses.configset.exists", extra={"config_set": name})
        else:
            raise
    topic_arn = settings().ses_notifications_topic_arn
    if not topic_arn:
        return
    try:
        ses.create_configuration_set_event_destination(
            ConfigurationSetName=name,
            EventDestination={
                "Name": "a2z-sns-notifications",
                "Enabled": True,
                "MatchingEventTypes": ["bounce", "complaint"],
                "SNSDestination": {"TopicARN": topic_arn},
            },
        )
        log.info("ses.eventdest.created", extra={"config_set": name})
    except ClientError as exc:
        if not _exists(exc, "EventDestinationAlreadyExists", "AlreadyExists"):
            raise


def main() -> None:
    create_tables()
    create_bucket()
    create_event_bus()
    create_omnichannel_queues()
    verify_ses_identity()
    create_sample_config_set()
    log.info("provision.complete", extra={"endpoint": settings().aws_endpoint_url})


if __name__ == "__main__":
    main()


# Convenience re-export so tests can provision without spawning a subprocess.
def provision_all() -> dict[str, Any]:
    main()
    return {"tables": list(settings().tables.values()), "bucket": s3_bucket_name()}
