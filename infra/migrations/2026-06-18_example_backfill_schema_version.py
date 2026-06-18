"""Example backfill: stamp a ``schema_version`` on org METADATA items.

Intent
------
Demonstrates the migration rules in CLAUDE.md §9. This is an **additive** change:
we add ``schema_version = 1`` to existing ``ORG#{id} / METADATA`` rows so future
readers can branch on it. New writes can start including it directly; this script
backfills history.

Properties (required of every migration):
  * **Idempotent / re-runnable** — uses a conditional write so an item that
    already has ``schema_version`` is skipped.
  * **Dry-run mode** — ``--dry-run`` reports what would change without writing.

Rollback
--------
The attribute is additive and harmless; rollback is "stop writing it" plus an
optional `REMOVE schema_version` pass. No destructive change is performed here.

Usage
-----
    python -m infra.migrations.2026-06-18_example_backfill_schema_version --dry-run
    python -m infra.migrations.2026-06-18_example_backfill_schema_version
"""

from __future__ import annotations

import argparse
import asyncio

from botocore.exceptions import ClientError

from app.config import settings
from app.core import clients
from app.core._ddb import from_item, to_item, to_value
from app.core.logging import get_logger

log = get_logger("migration.schema_version")

TARGET_VERSION = 1


async def _scan_org_metadata() -> list[dict[str, str]]:
    """Return PK/SK of all org METADATA items (paginated scan)."""
    table = settings().tables["membership"]
    keys: list[dict[str, str]] = []
    kwargs: dict[str, object] = {
        "TableName": table,
        "FilterExpression": "SK = :meta AND begins_with(PK, :org)",
        "ExpressionAttributeValues": {":meta": to_value("METADATA"), ":org": to_value("ORG#")},
        "ProjectionExpression": "PK, SK",
    }
    while True:
        resp = await clients.run_aws(clients.dynamodb().scan, **kwargs)
        for item in resp.get("Items", []):
            d = from_item(item)
            keys.append({"PK": d["PK"], "SK": d["SK"]})
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return keys


async def run(dry_run: bool) -> int:
    table = settings().tables["membership"]
    keys = await _scan_org_metadata()
    changed = 0
    for key in keys:
        if dry_run:
            log.info("migration.would_update", extra={"pk": key["PK"]})
            changed += 1
            continue
        try:
            await clients.run_aws(
                clients.dynamodb().update_item,
                TableName=table,
                Key=to_item(key),
                UpdateExpression="SET schema_version = :v",
                ConditionExpression="attribute_not_exists(schema_version)",
                ExpressionAttributeValues=to_item({":v": TARGET_VERSION}),
            )
            changed += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue  # already stamped — idempotent skip
            raise
    log.info("migration.complete", extra={"changed": changed, "dry_run": dry_run})
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, no writes")
    args = parser.parse_args()
    asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    main()
