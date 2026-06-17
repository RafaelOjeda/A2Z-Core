"""Canonical declarative specs for Core's AWS resources.

Single source of truth for DynamoDB tables, the S3 bucket, and the EventBridge
bus, imported by both ``scripts/create_local_resources.py`` and the test fixtures
so LocalStack mirrors exactly what Terragrunt provisions in AWS (CLAUDE.md §12).
Schemas follow Design §3.1. All tables are on-demand (PAY_PER_REQUEST) per the
cost decision in CLAUDE.md §10.

Each table spec is a ready-to-splat kwargs dict for ``dynamodb.create_table``,
minus the ``TableName`` (filled from config at provision time) and BillingMode.
"""

from __future__ import annotations

from typing import Any

from app.config import DDB_BILLING_MODE, settings


def _table(
    key_schema: list[dict[str, str]],
    attrs: list[dict[str, str]],
    *,
    gsis: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "KeySchema": key_schema,
        "AttributeDefinitions": attrs,
        "BillingMode": DDB_BILLING_MODE,
    }
    if gsis:
        spec["GlobalSecondaryIndexes"] = gsis
    return spec


def _gsi(name: str, pk: str, sk: str | None = None) -> dict[str, Any]:
    key_schema = [{"AttributeName": pk, "KeyType": "HASH"}]
    if sk:
        key_schema.append({"AttributeName": sk, "KeyType": "RANGE"})
    return {
        "IndexName": name,
        "KeySchema": key_schema,
        "Projection": {"ProjectionType": "ALL"},
    }


# logical name -> table definition (TableName injected at provision time).
def table_definitions() -> dict[str, dict[str, Any]]:
    return {
        # Single-table membership design (Design §3.1).
        # GSI1 lets us list all members of an org: ORG#{id} -> USER#{sub}.
        "membership": _table(
            key_schema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            attrs=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            gsis=[_gsi("GSI1", "GSI1PK", "GSI1SK")],
        ),
        # Append-only audit log. GSI1 by org+time, GSI2 by actor+time.
        "audit": _table(
            key_schema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            attrs=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "org_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
                {"AttributeName": "actor_id", "AttributeType": "S"},
            ],
            gsis=[
                _gsi("GSI1", "org_id", "timestamp"),
                _gsi("GSI2", "actor_id", "timestamp"),
            ],
        ),
        # Org settings — only ever queried by org_id.
        "settings": _table(
            key_schema=[{"AttributeName": "org_id", "KeyType": "HASH"}],
            attrs=[{"AttributeName": "org_id", "AttributeType": "S"}],
        ),
        # Email delivery tracking. Base lookup by message_id; GSI for per-org
        # recent-email queries (Design models this as an LSI, but it needs a
        # different partition key than the base table, so it's a GSI here).
        "email_events": _table(
            key_schema=[{"AttributeName": "message_id", "KeyType": "HASH"}],
            attrs=[
                {"AttributeName": "message_id", "AttributeType": "S"},
                {"AttributeName": "org_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            gsis=[_gsi("org-timestamp-index", "org_id", "timestamp")],
        ),
        # Suppression list — O(1) "is this email suppressed for this org?".
        "suppression": _table(
            key_schema=[
                {"AttributeName": "org_id", "KeyType": "HASH"},
                {"AttributeName": "email", "KeyType": "RANGE"},
            ],
            attrs=[
                {"AttributeName": "org_id", "AttributeType": "S"},
                {"AttributeName": "email", "AttributeType": "S"},
            ],
        ),
        # File metadata. GSI to list files by service within an org.
        "files": _table(
            key_schema=[
                {"AttributeName": "org_id", "KeyType": "HASH"},
                {"AttributeName": "key", "KeyType": "RANGE"},
            ],
            attrs=[
                {"AttributeName": "org_id", "AttributeType": "S"},
                {"AttributeName": "key", "AttributeType": "S"},
                {"AttributeName": "service_type", "AttributeType": "S"},
            ],
            gsis=[_gsi("service-index", "org_id", "service_type")],
        ),
    }


# Logical table name -> the DynamoDB attribute used as the TTL field, if any
# (CLAUDE.md §11 — TTLs set at write time, no cleanup jobs).
TTL_ATTRIBUTE = "ttl"
TABLES_WITH_TTL = {"audit", "email_events", "files"}


def s3_bucket_name() -> str:
    return settings().s3_bucket


def event_bus_name() -> str:
    return settings().event_bus_name
