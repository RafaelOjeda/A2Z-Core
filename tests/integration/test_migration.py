"""Verify the example migration is dry-run-capable and idempotent (CLAUDE.md §9)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from app.core import membership

pytestmark = pytest.mark.integration

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "infra/migrations/2026-06-18_example_backfill_schema_version.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("example_migration", _MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_migration_dry_run_and_idempotent(aws: None) -> None:
    mod = _load()
    await membership.create_org("Org One", "owner-1")
    await membership.create_org("Org Two", "owner-2")

    # Dry-run reports both orgs but writes nothing.
    assert await mod.run(dry_run=True) == 2

    # Real run stamps both.
    assert await mod.run(dry_run=False) == 2

    # Re-run is a no-op (idempotent conditional write).
    assert await mod.run(dry_run=False) == 0
