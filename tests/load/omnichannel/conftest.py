"""Fixtures for Omni-Channel load tests.

Re-exports the Postgres engine/session fixtures from the integration suite
rather than duplicating them -- the load tests need exactly the same
per-test engine handling (see the integration conftest for why the engine is
rebuilt per test).
"""

from __future__ import annotations

from tests.integration.omnichannel.conftest import _fresh_engine, session

__all__ = ["_fresh_engine", "session"]
