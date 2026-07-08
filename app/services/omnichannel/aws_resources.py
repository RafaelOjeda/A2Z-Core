"""Declarative provisioning for Omni-Channel's SQS queues.

Mirrors ``app/aws_resources.py``'s role for Core — single source of truth
for what ``scripts/create_local_resources.py`` and the Terragrunt
``sqs-omnichannel`` module both need to create — but scoped to this
service, not Core (root CLAUDE.md golden rule #3: Core never imports from
services/, so this lives here rather than in ``app/aws_resources.py``).
Sync, like the rest of the provisioning-script surface (``core.clients``
functions are async for the hot path; this is one-time bootstrap code).

Every queue gets a DLQ with ``maxReceiveCount=5``: any DLQ depth > 0 means a
message failed processing repeatedly and needs a human
(app/services/omnichannel/CLAUDE.md §10).
"""

from __future__ import annotations

import json

from botocore.exceptions import ClientError

from app.config import settings
from app.core import clients
from app.core.logging import get_logger

log = get_logger("omnichannel.aws_resources")

_MAX_RECEIVE_COUNT = 5


def _dlq_name(name: str) -> str:
    return f"{name}-dlq"


def _create_queue(name: str, attributes: dict[str, str] | None = None) -> str:
    """Create a queue (idempotent) and return its ARN."""
    sqs = clients.sqs()
    try:
        # boto3-stubs types Attributes keys as a Literal set; a plain dict of
        # queue-attribute names built from our own config isn't narrowed to
        # that Literal, same friction core.clients._client() already accepts.
        resp = sqs.create_queue(QueueName=name, Attributes=attributes or {})  # type: ignore[arg-type]
        url = resp["QueueUrl"]
        log.info("sqs.queue.created", extra={"queue": name})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "QueueAlreadyExists":
            url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
            log.info("sqs.queue.exists", extra={"queue": name})
        else:
            raise

    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])
    return attrs["Attributes"]["QueueArn"]


def create_queues() -> None:
    """Create every Omni-Channel inbound/outbound/events queue + its DLQ."""
    for name in settings().omnichannel_queue_names.values():
        dlq_arn = _create_queue(_dlq_name(name))
        redrive_policy = json.dumps(
            {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": _MAX_RECEIVE_COUNT}
        )
        _create_queue(name, attributes={"RedrivePolicy": redrive_policy})
