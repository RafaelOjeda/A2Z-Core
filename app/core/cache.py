"""In-process TTL cache — the shared caching primitive for single-box MVP.

Replaces the Redis-backed read-through caches Core and Omni-Channel used to
share (5-min settings/secrets TTLs, the 1h media signed-URL cache, the SES
config-set "exists" flag) now that everything runs in one process
(docs/architecture/single-box-mvp.md). Each cache site keeps its own
:class:`TTLCache` instance rather than sharing one dict, so one module's key
collisions can never bleed into another's -- the same isolation namespacing
(``secret:*``, ``mediaurl:*``, ...) gave for free under a shared Redis
client, just via separate Python objects instead of key prefixes.

Every :class:`TTLCache` (and any other clearable, e.g. a plain process-
lifetime ``set``) registers itself here so tests can reset all of them with
one call -- the in-process equivalent of each test getting a fresh fakeredis
server.
"""

from __future__ import annotations

import time
from collections.abc import Callable

_clearables: list[Callable[[], None]] = []


def register_clearable(clear: Callable[[], None]) -> None:
    """Register a zero-arg callable that resets some module-global cache state.

    Called once per cache site at import time; :func:`clear_all` runs every
    registered callable. Used by tests, not runtime code.
    """
    _clearables.append(clear)


def clear_all() -> None:
    """Reset every registered cache. Used by tests between runs."""
    for clear in _clearables:
        clear()


class TTLCache:
    """A minimal in-process string cache with per-entry expiry.

    Not thread-safe and not meant to be -- this whole platform runs as one
    process on one event loop (docs/architecture/single-box-mvp.md), so a
    plain dict needs no locking.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, str]] = {}  # key -> (expires_at, value)
        register_clearable(self.clear)

    def get(self, key: str) -> str | None:
        """Return the cached value, or ``None`` if absent or expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: str, *, ttl_seconds: float) -> None:
        """Store ``value`` under ``key`` for ``ttl_seconds``."""
        self._store[key] = (time.monotonic() + ttl_seconds, value)

    def ttl(self, key: str) -> float | None:
        """Seconds of validity remaining for ``key``, or ``None`` if absent/expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        remaining = entry[0] - time.monotonic()
        if remaining <= 0:
            return None
        return remaining

    def delete(self, key: str) -> None:
        """Remove ``key`` if present. A no-op if it isn't."""
        self._store.pop(key, None)

    def clear(self) -> None:
        """Drop every entry."""
        self._store.clear()
