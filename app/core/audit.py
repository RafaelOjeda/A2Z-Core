"""Audit — append-only event log for compliance and debugging (Design §2.5).

Every Core mutation logs here; reads generally do not. The log is append-only:
items are never updated or deleted. Each item carries a ``ttl`` (epoch seconds,
7 years out) so DynamoDB expires it for free — no cleanup job (CLAUDE.md §11).

Org scoping is non-negotiable: every query requires ``org_id`` and runs against
GSI1 (``org_id`` / ``timestamp``). There is no cross-org read path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.config import settings
from app.core import clients
from app.core._ddb import from_item, to_item, to_value
from app.core.exceptions import AuditError
from app.core.logging import get_logger

log = get_logger("core.audit")

_AUDIT_RETENTION = timedelta(days=365 * 7)  # 7 years (Design §3.1 / CLAUDE.md §11)


class ActionType(str, Enum):
    """Core-defined auditable actions. Services may also pass their own strings."""

    ORG_CREATED = "org.created"
    MEMBER_ADDED = "member.added"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"
    EMAIL_SENT = "email.sent"
    EMAIL_BOUNCED = "email.bounced"
    EMAIL_COMPLAINED = "email.complained"
    EMAIL_UNSUPPRESSED = "email.unsuppressed"
    FILE_UPLOADED = "file.uploaded"
    FILE_DELETED = "file.deleted"
    SETTINGS_CHANGED = "settings.changed"


class AuditEvent(BaseModel):
    """An auditable action."""

    event_id: str
    org_id: str
    timestamp: datetime
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


def _table() -> str:
    return settings().tables["audit"]


async def log_audit(
    org_id: str,
    actor_id: str,
    action: ActionType | str,
    resource_type: str,
    resource_id: str,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an auditable action to the log.

    Args:
        org_id: Org the action belongs to (always required).
        actor_id: Cognito sub of who performed it.
        action: An :class:`ActionType` or a service-defined dotted string.
        resource_type: What was affected (user, email, file, invoice, ...).
        resource_id: Id of the affected resource.
        metadata: Optional action-specific details.

    Returns:
        The persisted :class:`AuditEvent` (with generated id + timestamp).

    Raises:
        AuditError: The write failed.

    Performance:
        < 50ms (single DynamoDB put).
    """
    now = datetime.now(timezone.utc)
    event = AuditEvent(
        event_id=str(uuid.uuid4()),
        org_id=org_id,
        timestamp=now,
        actor_id=actor_id,
        action=action.value if isinstance(action, ActionType) else action,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata=metadata or {},
    )
    item = {
        "event_id": event.event_id,
        "org_id": event.org_id,
        "timestamp": now.isoformat(),
        "actor_id": event.actor_id,
        "action": event.action,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "metadata": event.metadata,
        "ttl": int((now + _AUDIT_RETENTION).timestamp()),
    }
    try:
        await clients.run_aws(clients.dynamodb().put_item, TableName=_table(), Item=to_item(item))
    except Exception as exc:  # noqa: BLE001 — re-raised as typed CoreError
        raise AuditError(f"Failed to write audit event: {exc}") from exc

    log.info(
        "audit.logged",
        extra={"org_id": org_id, "action": event.action, "actor_id": actor_id},
    )
    return event


async def get_audit_events(
    org_id: str,
    action_type: ActionType | str | None = None,
    actor_id: str | None = None,
    resource_id: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int = 100,
) -> list[AuditEvent]:
    """Query the audit log for an org, newest first.

    Args:
        org_id: Org to query (required — no cross-org reads).
        action_type: Optional action filter.
        actor_id: Optional actor filter.
        resource_id: Optional resource filter.
        from_time / to_time: Optional inclusive time bounds.
        limit: Max results (default 100).

    Returns:
        Matching events sorted by timestamp descending.

    Performance:
        < 500ms.
    """
    # ``timestamp`` is a DynamoDB reserved word -> alias it (only when used,
    # else DynamoDB rejects the unused name).
    names: dict[str, str] = {"#org": "org_id"}
    values: dict[str, Any] = {":org": org_id}
    key_expr = "#org = :org"
    if from_time or to_time:
        names["#ts"] = "timestamp"
    if from_time and to_time:
        key_expr += " AND #ts BETWEEN :from AND :to"
        values[":from"] = from_time.isoformat()
        values[":to"] = to_time.isoformat()
    elif from_time:
        key_expr += " AND #ts >= :from"
        values[":from"] = from_time.isoformat()
    elif to_time:
        key_expr += " AND #ts <= :to"
        values[":to"] = to_time.isoformat()

    filters: list[str] = []
    if action_type is not None:
        names["#action"] = "action"
        values[":action"] = (
            action_type.value if isinstance(action_type, ActionType) else action_type
        )
        filters.append("#action = :action")
    if actor_id is not None:
        names["#actor"] = "actor_id"
        values[":actor"] = actor_id
        filters.append("#actor = :actor")
    if resource_id is not None:
        names["#rid"] = "resource_id"
        values[":rid"] = resource_id
        filters.append("#rid = :rid")

    query: dict[str, Any] = {
        "TableName": _table(),
        "IndexName": "GSI1",
        "KeyConditionExpression": key_expr,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": {k: to_value(v) for k, v in values.items()},
        "ScanIndexForward": False,  # newest first
        "Limit": limit,
    }
    if filters:
        query["FilterExpression"] = " AND ".join(filters)

    try:
        resp = await clients.run_aws(clients.dynamodb().query, **query)
    except Exception as exc:  # noqa: BLE001
        raise AuditError(f"Failed to query audit log: {exc}") from exc

    return [_to_event(from_item(it)) for it in resp.get("Items", [])]


def _to_event(data: dict[str, Any]) -> AuditEvent:
    return AuditEvent(
        event_id=data["event_id"],
        org_id=data["org_id"],
        timestamp=datetime.fromisoformat(data["timestamp"]),
        actor_id=data["actor_id"],
        action=data["action"],
        resource_type=data["resource_type"],
        resource_id=data["resource_id"],
        metadata=data.get("metadata", {}),
    )
