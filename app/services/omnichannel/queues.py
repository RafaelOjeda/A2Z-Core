"""SQS queueing for Omni-Channel's shared inbound/outbound pipelines (§5.6, §12).

One inbound queue for every channel, one outbound queue -- never per-channel
queues (§5.2 extensibility invariant: a new channel touches adapters/ + the
registry, never routing or infra). Queue URLs are resolved once per queue
name and cached in-process (``GetQueueUrl`` is cheap but there's no reason to
call it on every send/receive).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.core import clients
from app.core.logging import get_logger

log = get_logger("omnichannel.queues")

_queue_url_cache: dict[str, str] = {}


@dataclass
class QueueMessage:
    body: dict[str, Any]
    attributes: dict[str, str]
    receipt_handle: str
    receive_count: int


def reset_queue_url_cache() -> None:
    """Clear cached queue URLs. Used by tests after re-provisioning queues."""
    _queue_url_cache.clear()


async def _queue_url(name: str) -> str:
    cached = _queue_url_cache.get(name)
    if cached is not None:
        return cached
    resp = await clients.run_aws(clients.sqs().get_queue_url, QueueName=name)
    url = str(resp["QueueUrl"])
    _queue_url_cache[name] = url
    return url


async def _send(queue_name: str, body: dict[str, Any], message_attributes: dict[str, str]) -> str:
    url = await _queue_url(queue_name)
    attrs = {k: {"DataType": "String", "StringValue": v} for k, v in message_attributes.items()}
    resp = await clients.run_aws(
        clients.sqs().send_message,
        QueueUrl=url,
        MessageBody=json.dumps(body, default=str),
        MessageAttributes=attrs,
    )
    return str(resp["MessageId"])


async def _receive(
    queue_name: str, *, max_messages: int, wait_time_seconds: int
) -> list[QueueMessage]:
    url = await _queue_url(queue_name)
    resp = await clients.run_aws(
        clients.sqs().receive_message,
        QueueUrl=url,
        MaxNumberOfMessages=max_messages,
        WaitTimeSeconds=wait_time_seconds,
        MessageAttributeNames=["All"],
        AttributeNames=["ApproximateReceiveCount"],
    )
    messages: list[QueueMessage] = []
    for raw in resp.get("Messages", []):
        attrs = {k: v["StringValue"] for k, v in raw.get("MessageAttributes", {}).items()}
        receive_count = int(raw.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
        messages.append(
            QueueMessage(
                body=json.loads(raw["Body"]),
                attributes=attrs,
                receipt_handle=raw["ReceiptHandle"],
                receive_count=receive_count,
            )
        )
    return messages


async def _delete(queue_name: str, receipt_handle: str) -> None:
    url = await _queue_url(queue_name)
    await clients.run_aws(clients.sqs().delete_message, QueueUrl=url, ReceiptHandle=receipt_handle)


async def enqueue_inbound(
    *, org_id: str, channel_type: str, connection_id: str, raw_payload: dict[str, Any]
) -> str:
    """Enqueue an already-signature-verified inbound webhook payload (§5.6).

    Message attributes (``channel_type``/``org_id``/``connection_id``) let
    the worker (and any future per-channel monitoring) filter without
    parsing the body -- one shared queue for every channel.
    """
    message_id = await _send(
        settings().omnichannel_inbound_queue,
        {"raw_payload": raw_payload},
        {"channel_type": channel_type, "org_id": org_id, "connection_id": connection_id},
    )
    log.info(
        "omnichannel.inbound.enqueued",
        extra={"org_id": org_id, "channel_type": channel_type, "connection_id": connection_id},
    )
    return message_id


async def receive_inbound(
    *, max_messages: int = 10, wait_time_seconds: int = 0
) -> list[QueueMessage]:
    return await _receive(
        settings().omnichannel_inbound_queue,
        max_messages=max_messages,
        wait_time_seconds=wait_time_seconds,
    )


async def delete_inbound(receipt_handle: str) -> None:
    await _delete(settings().omnichannel_inbound_queue, receipt_handle)


async def enqueue_outbound(*, org_id: str, message_id: str) -> str:
    """Enqueue a queued outbound message for the worker to send (§5.6).

    Deliberately minimal payload: the worker reloads the ``Message`` row (and
    its conversation/connection) from Postgres by id rather than duplicating
    that state onto the queue.
    """
    sqs_message_id = await _send(
        settings().omnichannel_outbound_queue,
        {"message_id": message_id},
        {"org_id": org_id},
    )
    log.info("omnichannel.outbound.enqueued", extra={"org_id": org_id, "message_id": message_id})
    return sqs_message_id


async def receive_outbound(
    *, max_messages: int = 10, wait_time_seconds: int = 0
) -> list[QueueMessage]:
    return await _receive(
        settings().omnichannel_outbound_queue,
        max_messages=max_messages,
        wait_time_seconds=wait_time_seconds,
    )


async def delete_outbound(receipt_handle: str) -> None:
    await _delete(settings().omnichannel_outbound_queue, receipt_handle)
