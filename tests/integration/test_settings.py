"""Integration tests for core.settings (moto + fakeredis), incl. Design §4.4."""

from __future__ import annotations

import pytest

from app.core import audit, settings
from app.core.audit import ActionType
from app.core.exceptions import SettingsError

pytestmark = pytest.mark.integration


async def test_defaults_when_absent(aws: None) -> None:
    s = await settings.get_org_settings("brand-new")
    assert s.timezone == "UTC"
    assert s.currency == "USD"
    assert s.invoice_number_prefix == "INV-"


async def test_settings_and_invoice_numbering(aws: None) -> None:
    """Design §4.4: update settings -> atomic invoice numbering -> audit."""
    org_id = "test-org-999"
    updated = await settings.set_org_settings(
        org_id,
        {"timezone": "America/Los_Angeles", "invoice_number_prefix": "INV-2026-"},
        "auth0|user999",
    )
    assert updated.timezone == "America/Los_Angeles"
    assert updated.invoice_number_prefix == "INV-2026-"

    assert await settings.get_next_invoice_number(org_id, "INV-2026-") == "INV-2026-1"
    assert await settings.get_next_invoice_number(org_id, "INV-2026-") == "INV-2026-2"

    events = await audit.get_audit_events(org_id, action_type=ActionType.SETTINGS_CHANGED)
    assert len(events) >= 1


async def test_unknown_field_rejected(aws: None) -> None:
    with pytest.raises(SettingsError):
        await settings.set_org_settings("org", {"not_a_field": 1}, "user")


async def test_empty_changes_rejected(aws: None) -> None:
    with pytest.raises(SettingsError):
        await settings.set_org_settings("org", {}, "user")


async def test_cache_round_trip_and_invalidation(aws: None) -> None:
    org_id = "cache-org"
    # Prime the cache.
    first = await settings.get_org_settings(org_id)
    assert first.currency == "USD"
    # Write should invalidate; next read reflects the change.
    await settings.set_org_settings(org_id, {"currency": "EUR"}, "user")
    assert (await settings.get_org_settings(org_id)).currency == "EUR"


async def test_cross_org_settings_isolation(aws: None) -> None:
    await settings.set_org_settings("org-a", {"currency": "GBP"}, "user")
    b = await settings.get_org_settings("org-b")
    assert b.currency == "USD"  # untouched default, not org-a's value
