"""Cognito Post-Confirmation trigger — create the Core user record (CLAUDE.md §5).

Wired to the User Pool's Post Confirmation trigger. Calls
``create_user_if_not_exists`` (idempotent; Cognito may retry the trigger).

Critically, it **must never block signup** on a transient Core failure: on error
we log + emit a CloudWatch metric and still return the event so Cognito completes
signup. A reconciliation job can backfill any missing user rows.

First-login org bootstrap is intentionally **not** done here — it happens on the
first authenticated request, keeping this Lambda minimal and signup fast.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.core.membership import create_user_if_not_exists

log = get_logger("lambda.cognito_post_confirm")


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Cognito Post-Confirmation Lambda entrypoint."""
    attrs = event.get("request", {}).get("userAttributes", {})
    sub = attrs.get("sub")
    email = attrs.get("email")

    if sub and email:
        try:
            asyncio.run(create_user_if_not_exists(sub, email))
            log.info("cognito.user.provisioned", extra={"user_id": sub})
        except Exception as exc:  # noqa: BLE001 — must not block signup
            # Emit a metric marker (a CloudWatch metric filter keys on this
            # event name) and let signup proceed.
            log.error(
                "cognito.post_confirm.failed",
                extra={"user_id": sub, "error": str(exc), "metric": "PostConfirmFailure"},
            )

    # Always return the event — Cognito requires it to finish signup.
    return event
