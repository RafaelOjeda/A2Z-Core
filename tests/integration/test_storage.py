"""Integration tests for core.storage (moto S3 + DynamoDB), incl. Design §4.3."""

from __future__ import annotations

import pytest

from app.core import audit, storage
from app.core.audit import ActionType
from app.core.exceptions import FileTooLargeError, StorageError, StorageNotFoundError

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.4\nfake content"


async def test_upload_download_list_and_audit(aws: None) -> None:
    """Design §4.3: upload -> signed URL -> download -> list -> audit."""
    org_id, user = "test-org-789", "auth0|user789"
    result = await storage.upload_file(
        org_id, "invoicing", "invoice-1054.pdf", PDF, "application/pdf", user,
        ttl_days=30,
    )
    assert result.key.startswith(f"{org_id}/invoicing/")
    assert result.size_bytes == len(PDF)
    assert result.signed_url.startswith("https://")

    assert await storage.download_file(org_id, result.key) == PDF

    files = await storage.list_files(org_id, service_type="invoicing")
    assert len(files) == 1 and files[0].filename == "invoice-1054.pdf"

    events = await audit.get_audit_events(
        org_id, action_type=ActionType.FILE_UPLOADED, resource_id=result.key
    )
    assert len(events) >= 1


async def test_file_too_large_rejected(aws: None) -> None:
    big = b"x" * (storage.MAX_FILE_BYTES + 1)
    with pytest.raises(FileTooLargeError):
        await storage.upload_file("o", "s", "big.bin", big, "application/octet-stream", "u")


async def test_cross_org_download_denied(aws: None) -> None:
    r = await storage.upload_file("org-a", "invoicing", "f.pdf", PDF, "application/pdf", "u")
    # org-b must not be able to read org-a's key.
    with pytest.raises(StorageError):
        await storage.download_file("org-b", r.key)


async def test_get_metadata_missing(aws: None) -> None:
    with pytest.raises(StorageNotFoundError):
        await storage.get_file_metadata("org-a", "org-a/invoicing/missing.pdf")


async def test_soft_delete_removes_from_listing(aws: None) -> None:
    org_id = "org-del"
    r = await storage.upload_file(org_id, "invoicing", "f.pdf", PDF, "application/pdf", "u")
    await storage.delete_file(org_id, r.key, "u")
    assert await storage.list_files(org_id) == []
    events = await audit.get_audit_events(org_id, action_type=ActionType.FILE_DELETED)
    assert len(events) >= 1


async def test_list_filters_by_service_and_prefix(aws: None) -> None:
    org_id = "org-filter"
    await storage.upload_file(org_id, "invoicing", "inv-1.pdf", PDF, "application/pdf", "u")
    await storage.upload_file(org_id, "omnichannel", "msg-1.jpg", PDF, "image/jpeg", "u")

    invoicing = await storage.list_files(org_id, service_type="invoicing")
    assert {f.service_type for f in invoicing} == {"invoicing"}

    prefixed = await storage.list_files(org_id, filename_prefix="inv-")
    assert len(prefixed) == 1 and prefixed[0].filename == "inv-1.pdf"
