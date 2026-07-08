"""Phase 0 smoke test: the local provisioner stands up every Core resource
(and, since Step 2/5, Omni-Channel's Postgres-adjacent AWS resources too —
the SQS queues — since both share the same provisioning script)."""

from __future__ import annotations

import pytest

from app.config import settings
from app.core import clients

pytestmark = pytest.mark.integration


def test_all_tables_bucket_and_bus_created(aws: None) -> None:
    ddb = clients.dynamodb()
    existing = set(ddb.list_tables()["TableNames"])
    assert set(settings().tables.values()) <= existing

    buckets = {b["Name"] for b in clients.s3().list_buckets()["Buckets"]}
    assert settings().s3_bucket in buckets

    buses = {b["Name"] for b in clients.eventbridge().list_event_buses()["EventBuses"]}
    assert settings().event_bus_name in buses


def test_omnichannel_queues_and_dlqs_created(aws: None) -> None:
    sqs = clients.sqs()
    queue_urls = sqs.list_queues().get("QueueUrls", [])
    existing_names = set()
    for url in queue_urls:
        attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])
        existing_names.add(attrs["Attributes"]["QueueArn"].rsplit(":", 1)[-1])

    for name in settings().omnichannel_queue_names.values():
        assert name in existing_names
        assert f"{name}-dlq" in existing_names


def test_provisioning_is_idempotent(aws: None) -> None:
    from scripts.create_local_resources import main as provision

    # Re-running against already-created resources must not raise.
    provision()
