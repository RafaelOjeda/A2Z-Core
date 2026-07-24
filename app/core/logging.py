"""Structured JSON logging — deliberately lean.

CloudWatch ingestion is billed per GB, so we log *significant events only*, one
compact JSON line each (CLAUDE.md §4 and the cost note). Mutations and failures
log; hot-path reads do not. Never pass JWTs, full email bodies, or PII beyond
what an operator strictly needs to debug.

Usage:
    from app.core.logging import get_logger
    log = get_logger(__name__)
    log.info("member.added", extra={"org_id": org_id, "actor_id": actor_id})
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

from app.config import settings

# Threaded through a request so every log line can be correlated.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Keys we never emit even if a caller passes them by mistake.
_REDACT_KEYS = frozenset({"token", "jwt", "authorization", "password", "secret"})

# LogRecord attributes that ``extra=`` must not overwrite (logging raises if it
# does). We defensively rename any colliding key to ``<key>_`` instead.
_RESERVED_LOG_KEYS = frozenset({
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "asctime",
    "taskName",
})


class _JsonFormatter(logging.Formatter):
    """Render a log record as a single compact JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid

        # Attributes attached via `extra=` land directly on the record.
        standard = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
        standard |= {"message", "asctime"}
        for key, value in record.__dict__.items():
            if key in standard or key.startswith("_"):
                continue
            payload[key] = "***" if key.lower() in _REDACT_KEYS else value

        return json.dumps(payload, default=str, separators=(",", ":"))


_configured = False


def _configure_root() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger("a2z")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings().log_level.upper())
    root.propagate = False
    _configured = True


class _SafeAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Renames any ``extra`` key that would collide with a LogRecord attribute."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        extra = kwargs.get("extra")
        if extra:
            kwargs["extra"] = {
                (f"{k}_" if k in _RESERVED_LOG_KEYS else k): v for k, v in extra.items()
            }
        return msg, kwargs


def get_logger(name: str) -> _SafeAdapter:
    """Return a namespaced logger emitting compact JSON lines.

    Wrapped in an adapter that defends against ``extra`` keys colliding with
    reserved LogRecord attributes (which logging would otherwise reject).
    """
    _configure_root()
    return _SafeAdapter(logging.getLogger(f"a2z.{name}"), {})
