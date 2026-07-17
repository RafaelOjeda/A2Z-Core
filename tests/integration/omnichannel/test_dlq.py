"""Integration tests for DLQ wiring (§5.6 "bounded retry ... then DLQ + alarm").

Runs against moto's real SQS redrive implementation -- a message received more
than ``maxReceiveCount`` times is moved to the DLQ by SQS itself. These tests
exist because the DLQ is only useful if it can actually receive anything: the
§11 alarm watches DLQ depth > 0, so a worker that retires exhausted messages
itself would silently disarm it.
"""

from __future__ import annotations

import json

import pytest

from app.config import SQS_MAX_RECEIVE_COUNT, settings
from app.core import clients
from app.services.omnichannel import queues

pytestmark = pytest.mark.integration


async def _queue_url(name: str) -> str:
    resp = await clients.run_aws(clients.sqs().get_queue_url, QueueName=name)
    return str(resp["QueueUrl"])


async def _depth(name: str) -> int:
    url = await _queue_url(name)
    resp = await clients.run_aws(
        clients.sqs().get_queue_attributes,
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages"],
    )
    return int(resp["Attributes"]["ApproximateNumberOfMessages"])


async def _redrive_policy(name: str) -> dict[str, object]:
    url = await _queue_url(name)
    resp = await clients.run_aws(
        clients.sqs().get_queue_attributes, QueueUrl=url, AttributeNames=["RedrivePolicy"]
    )
    policy: dict[str, object] = json.loads(resp["Attributes"]["RedrivePolicy"])
    return policy


@pytest.mark.parametrize("queue_attr", ["omnichannel_inbound_queue", "omnichannel_outbound_queue"])
async def test_queues_have_redrive_policy(aws: None, queue_attr: str) -> None:
    """Both shared queues redrive to their DLQ at the configured threshold."""
    name = getattr(settings(), queue_attr)
    policy = await _redrive_policy(name)

    assert policy["maxReceiveCount"] == SQS_MAX_RECEIVE_COUNT
    assert str(policy["deadLetterTargetArn"]).endswith("-dlq")


async def test_dlqs_start_empty(aws: None) -> None:
    assert await _depth(settings().omnichannel_inbound_dlq) == 0
    assert await _depth(settings().omnichannel_outbound_dlq) == 0


async def _exhaust_receives(queue_name: str) -> None:
    """Receive a message past the redrive threshold without ever deleting it.

    Mirrors what a permanently-failing send does. The queue carries SQS's
    default 30s visibility timeout (that timeout *is* the retry backoff §5.6
    calls for), so each receive is followed by resetting visibility to 0 --
    simulating the timeout expiring instead of sleeping through it.
    """
    url = await _queue_url(queue_name)
    for _ in range(SQS_MAX_RECEIVE_COUNT):
        messages = await queues.receive_outbound(max_messages=1)
        assert messages, "expected SQS to redeliver below the redrive threshold"
        await clients.run_aws(
            clients.sqs().change_message_visibility,
            QueueUrl=url,
            ReceiptHandle=messages[0].receipt_handle,
            VisibilityTimeout=0,
        )


async def test_message_redrives_to_dlq_after_max_receives(aws: None) -> None:
    """The end-to-end guarantee: a message nobody deletes lands on the DLQ.

    This is what makes the §11 DLQ-depth alarm reachable -- and why the worker
    must NOT delete an exhausted send itself.
    """
    await queues.enqueue_outbound(org_id="org-a", message_id="msg-1")
    await _exhaust_receives(settings().omnichannel_outbound_queue)

    # The receive that exceeds maxReceiveCount is the one SQS redrives on.
    assert await queues.receive_outbound(max_messages=1) == []

    assert await _depth(settings().omnichannel_outbound_dlq) == 1
    # And it's gone from the main queue -- SQS retired it, we didn't.
    assert await _depth(settings().omnichannel_outbound_queue) == 0


async def test_dlq_preserves_message_body(aws: None) -> None:
    """A redriven message keeps its payload, so an operator can inspect/replay."""
    await queues.enqueue_outbound(org_id="org-a", message_id="msg-42")
    await _exhaust_receives(settings().omnichannel_outbound_queue)
    await queues.receive_outbound(max_messages=1)

    dlq_url = await _queue_url(settings().omnichannel_outbound_dlq)
    resp = await clients.run_aws(
        clients.sqs().receive_message, QueueUrl=dlq_url, MaxNumberOfMessages=1
    )
    body = json.loads(resp["Messages"][0]["Body"])
    assert body["message_id"] == "msg-42"
