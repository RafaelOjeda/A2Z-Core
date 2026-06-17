"""Settings — org-level configuration, cached (Design §2.6).

Reads go through Redis (5-min TTL) and fall back to DynamoDB, applying defaults
for any missing field. Writes update DynamoDB, invalidate the cache, and audit
``settings.changed``. The invoice counter is an atomic DynamoDB ``ADD`` so it
never collides or skips.

Deviation from Design §2.6: ``get_next_invoice_number`` is ``async`` here (the
design shows it sync). It performs a DynamoDB write, so per the golden rule
"async for all I/O" (CLAUDE.md §4) we don't block the event loop on it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from app.config import settings as app_settings
from app.core import clients
from app.core._ddb import from_item, to_item, to_value
from app.core.audit import ActionType, log_audit
from app.core.exceptions import SettingsError
from app.core.logging import get_logger

log = get_logger("core.settings")

_CACHE_TTL_SECONDS = 300  # 5 min (Design §2.6)
_CACHE_PREFIX = "settings:"

# Fields a caller may set, with their defaults applied on read.
_DEFAULTS: dict[str, Any] = {
    "timezone": "UTC",
    "currency": "USD",
    "locale": "en_US",
    "domain": "",
    "invoice_number_prefix": "INV-",
    "next_invoice_number": 1,
    "plan_tier": "free",
    "sender_name": "",
    "metadata": {},
}
_MUTABLE_FIELDS = frozenset(_DEFAULTS)


class OrgSettings(BaseModel):
    org_id: str
    timezone: str = "UTC"
    currency: str = "USD"
    locale: str = "en_US"
    domain: str = ""
    invoice_number_prefix: str = "INV-"
    next_invoice_number: int = 1
    plan_tier: str = "free"
    sender_name: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime | None = None


def _table() -> str:
    return app_settings().tables["settings"]


def _cache_key(org_id: str) -> str:
    return f"{_CACHE_PREFIX}{org_id}"


async def get_org_settings(org_id: str) -> OrgSettings:
    """Return org settings, defaults applied (Design §2.6).

    Reads from Redis (5-min TTL), falling back to DynamoDB. Performance: < 50ms.
    """
    redis = clients.redis_client()
    cached = await redis.get(_cache_key(org_id))
    if cached:
        return OrgSettings.model_validate_json(cached)

    resp = await clients.run_aws(
        clients.dynamodb().get_item,
        TableName=_table(),
        Key=to_item({"org_id": org_id}),
    )
    data = from_item(resp["Item"]) if resp.get("Item") else {}
    merged = {**_DEFAULTS, **{k: v for k, v in data.items() if k != "org_id"}}
    result = OrgSettings(org_id=org_id, **merged)

    await redis.set(_cache_key(org_id), result.model_dump_json(), ex=_CACHE_TTL_SECONDS)
    return result


async def set_org_settings(
    org_id: str, changes: dict[str, Any], changed_by: str
) -> OrgSettings:
    """Partially update org settings (Design §2.6).

    Validates field names, writes DynamoDB, invalidates the cache, and logs
    ``settings.changed``. Raises SettingsError on an unknown field.
    Performance: < 100ms.
    """
    unknown = set(changes) - _MUTABLE_FIELDS
    if unknown:
        raise SettingsError(f"Unknown settings field(s): {sorted(unknown)}")
    if not changes:
        raise SettingsError("No changes provided")

    now = datetime.now(timezone.utc)
    set_clause = "SET updated_at = :updated_at"
    names: dict[str, str] = {}
    values: dict[str, Any] = {":updated_at": now.isoformat()}
    for i, (field, value) in enumerate(changes.items()):
        # Alias field names defensively (currency/timezone etc. are safe, but
        # this keeps us clear of any future reserved word).
        ph_name, ph_val = f"#f{i}", f":v{i}"
        names[ph_name] = field
        values[ph_val] = value
        set_clause += f", {ph_name} = {ph_val}"

    try:
        await clients.run_aws(
            clients.dynamodb().update_item,
            TableName=_table(),
            Key=to_item({"org_id": org_id}),
            UpdateExpression=set_clause,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={k: to_value(v) for k, v in values.items()},
        )
    except ClientError as exc:
        raise SettingsError(f"Failed to update settings: {exc}") from exc

    await clients.redis_client().delete(_cache_key(org_id))
    await log_audit(
        org_id, changed_by, ActionType.SETTINGS_CHANGED, "settings", org_id,
        {"changed_fields": sorted(changes)},
    )
    log.info("settings.changed", extra={"org_id": org_id, "fields": sorted(changes)})
    return await get_org_settings(org_id)


async def get_next_invoice_number(org_id: str, prefix: str) -> str:
    """Atomically increment and return the next invoice number (Design §2.6).

    The counter is a DynamoDB ``ADD`` — no gaps, no collisions. First call for an
    org returns ``{prefix}1``. Performance: < 50ms.
    """
    try:
        resp = await clients.run_aws(
            clients.dynamodb().update_item,
            TableName=_table(),
            Key=to_item({"org_id": org_id}),
            UpdateExpression="ADD next_invoice_number :one",
            ExpressionAttributeValues=to_item({":one": 1}),
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as exc:
        raise SettingsError(f"Failed to allocate invoice number: {exc}") from exc

    # Counter write changes settings -> drop the cached copy.
    await clients.redis_client().delete(_cache_key(org_id))
    n = int(from_item(resp["Attributes"])["next_invoice_number"])
    return f"{prefix}{n}"
