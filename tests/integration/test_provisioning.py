"""Phase 0 smoke test: the local provisioner stands up every Core resource."""

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


def test_provisioning_is_idempotent(aws: None) -> None:
    from scripts.create_local_resources import main as provision

    # Re-running against already-created resources must not raise.
    provision()
