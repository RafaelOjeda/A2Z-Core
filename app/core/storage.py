"""Storage — org-scoped file storage on S3 with DynamoDB metadata (Design §2.4).

The bucket is private; everything is accessed via short-lived signed URLs. Org
isolation is structural: every S3 key is prefixed with ``{org_id}/`` and every
read re-checks that prefix, so there is no path to another org's data. File
metadata lives in the ``files`` table for listing/querying; deletes are soft
(``is_deleted``) to preserve the audit trail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import ClientError
from pydantic import BaseModel

from app.config import settings
from app.core import clients
from app.core._ddb import from_item, to_item, to_value
from app.core.audit import ActionType, log_audit
from app.core.exceptions import FileTooLargeError, StorageError, StorageNotFoundError
from app.core.logging import get_logger

log = get_logger("core.storage")

MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MB (Design §2.4)
_SIGNED_URL_TTL = 3600  # 1 hour


class StoredFile(BaseModel):
    key: str
    filename: str
    url: str
    signed_url: str
    size_bytes: int
    mime_type: str
    uploaded_at: datetime
    uploaded_by: str
    service_type: str | None = None


def _table() -> str:
    return settings().tables["files"]


def _bucket() -> str:
    return settings().s3_bucket


def generate_signed_url(key: str, expires_in: int = _SIGNED_URL_TTL) -> str:
    """Return a presigned GET URL for an S3 key (Design §2.4).

    Local signing only (no network). Performance: < 50ms.
    """
    return clients.s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": key},
        ExpiresIn=expires_in,
    )


async def upload_file(
    org_id: str,
    service_type: str,
    filename: str,
    content: bytes,
    mime_type: str,
    uploaded_by: str,
    ttl_days: int | None = None,
) -> StoredFile:
    """Upload a file to S3 and record its metadata (Design §2.4).

    Raises FileTooLargeError if content exceeds 100 MB. Logs ``file.uploaded``.
    Performance: < 1s (size-dependent).
    """
    if len(content) > MAX_FILE_BYTES:
        raise FileTooLargeError(f"File is {len(content)} bytes; max is {MAX_FILE_BYTES}")

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S-%f")
    key = f"{org_id}/{service_type}/{ts}_{filename}"

    try:
        await clients.run_aws(
            clients.s3().put_object,
            Bucket=_bucket(),
            Key=key,
            Body=content,
            ContentType=mime_type,
        )
    except ClientError as exc:
        raise StorageError(f"Failed to upload file: {exc}") from exc

    meta: dict[str, Any] = {
        "org_id": org_id,
        "key": key,
        "filename": filename,
        "size_bytes": len(content),
        "mime_type": mime_type,
        "uploaded_at": now.isoformat(),
        "uploaded_by": uploaded_by,
        "service_type": service_type,
        "is_deleted": False,
    }
    if ttl_days is not None:
        # Epoch seconds for DynamoDB TTL; matching S3 object expiry is handled
        # by an S3 lifecycle rule (CLAUDE.md §11 / docs/retention.md).
        meta["ttl"] = int((now + timedelta(days=ttl_days)).timestamp())

    await clients.run_aws(clients.dynamodb().put_item, TableName=_table(), Item=to_item(meta))
    await log_audit(
        org_id,
        uploaded_by,
        ActionType.FILE_UPLOADED,
        "file",
        key,
        {"filename": filename, "size_bytes": len(content), "mime_type": mime_type},
    )
    log.info("file.uploaded", extra={"org_id": org_id, "size_bytes": len(content)})

    signed = generate_signed_url(key)
    return StoredFile(
        key=key,
        filename=filename,
        url=signed,
        signed_url=signed,
        size_bytes=len(content),
        mime_type=mime_type,
        uploaded_at=now,
        uploaded_by=uploaded_by,
        service_type=service_type,
    )


def _assert_org_scope(org_id: str, key: str) -> None:
    if not key.startswith(f"{org_id}/"):
        # Never serve another org's object, even if the key is guessed.
        raise StorageError("File does not belong to this org")


async def download_file(org_id: str, key: str) -> bytes:
    """Download a file's bytes, enforcing org scope (Design §2.4).

    Raises StorageError on cross-org access, StorageNotFoundError if missing.
    Performance: < 500ms (size-dependent).
    """
    _assert_org_scope(org_id, key)
    try:
        resp = await clients.run_aws(clients.s3().get_object, Bucket=_bucket(), Key=key)
        return bytes(resp["Body"].read())
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            raise StorageNotFoundError(f"File not found: {key}") from exc
        raise StorageError(f"Failed to download file: {exc}") from exc


async def get_file_metadata(org_id: str, key: str) -> StoredFile:
    """Return file metadata without downloading it (Design §2.4). < 50ms."""
    _assert_org_scope(org_id, key)
    resp = await clients.run_aws(
        clients.dynamodb().get_item,
        TableName=_table(),
        Key=to_item({"org_id": org_id, "key": key}),
    )
    if not resp.get("Item"):
        raise StorageNotFoundError(f"File metadata not found: {key}")
    return _to_stored_file(from_item(resp["Item"]))


async def delete_file(org_id: str, key: str, deleted_by: str) -> None:
    """Delete a file from S3 and soft-delete its metadata (Design §2.4).

    Logs ``file.deleted``. Soft delete keeps the row for the audit trail.
    """
    _assert_org_scope(org_id, key)
    try:
        await clients.run_aws(clients.s3().delete_object, Bucket=_bucket(), Key=key)
    except ClientError as exc:
        raise StorageError(f"Failed to delete file: {exc}") from exc

    await clients.run_aws(
        clients.dynamodb().update_item,
        TableName=_table(),
        Key=to_item({"org_id": org_id, "key": key}),
        UpdateExpression="SET is_deleted = :true",
        ExpressionAttributeValues=to_item({":true": True}),
    )
    await log_audit(org_id, deleted_by, ActionType.FILE_DELETED, "file", key)
    log.info("file.deleted", extra={"org_id": org_id})


async def list_files(
    org_id: str,
    service_type: str | None = None,
    filename_prefix: str | None = None,
) -> list[StoredFile]:
    """List an org's (non-deleted) files (Design §2.4). < 200ms."""
    if service_type is not None:
        query: dict[str, Any] = {
            "TableName": _table(),
            "IndexName": "service-index",
            "KeyConditionExpression": "org_id = :o AND service_type = :s",
            "ExpressionAttributeValues": {":o": to_value(org_id), ":s": to_value(service_type)},
        }
    else:
        query = {
            "TableName": _table(),
            "KeyConditionExpression": "org_id = :o",
            "ExpressionAttributeValues": {":o": to_value(org_id)},
        }
    resp = await clients.run_aws(clients.dynamodb().query, **query)

    files: list[StoredFile] = []
    for item in resp.get("Items", []):
        data = from_item(item)
        if data.get("is_deleted"):
            continue
        if filename_prefix and not str(data.get("filename", "")).startswith(filename_prefix):
            continue
        files.append(_to_stored_file(data))
    return files


def _to_stored_file(data: dict[str, Any]) -> StoredFile:
    signed = generate_signed_url(data["key"])
    return StoredFile(
        key=data["key"],
        filename=data["filename"],
        url=signed,
        signed_url=signed,
        size_bytes=int(data["size_bytes"]),
        mime_type=data["mime_type"],
        uploaded_at=datetime.fromisoformat(data["uploaded_at"]),
        uploaded_by=data["uploaded_by"],
        service_type=data.get("service_type"),
    )
