"""Health endpoint — checks DynamoDB + Postgres reachability (DoD, CLAUDE.md §15).

Single-box MVP (docs/architecture/single-box-mvp.md): Redis is gone, and
Postgres -- previously not Core's concern at all -- is now the thing whose
loss actually matters on this box (it's a container next to the app, not a
managed RDS instance with its own health story). The Postgres check reuses
Omni-Channel's engine (any service's would do; they all point at the same
``DATABASE_URL``/instance, just different schemas) rather than opening a
second one here -- this is a router, not a `core/` module, so importing a
service is fine (root CLAUDE.md's "Core never imports from services" rule is
about `app/core/`, not `app/routers/`; `routers/omnichannel.py` already
imports service code the same way).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core import clients
from app.core.logging import get_logger
from app.services.omnichannel import db as omnichannel_db

router = APIRouter(tags=["health"])
log = get_logger("router.health")


@router.get("/health")
async def health() -> JSONResponse:
    """Liveness/readiness: 200 when DynamoDB and Postgres are reachable, else 503."""
    checks: dict[str, str] = {}
    healthy = True

    try:
        await clients.run_aws(clients.dynamodb().list_tables)
        checks["dynamodb"] = "ok"
    except Exception:  # noqa: BLE001 — health probe must not raise
        checks["dynamodb"] = "error"
        healthy = False

    try:
        async with omnichannel_db.engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:  # noqa: BLE001
        checks["postgres"] = "error"
        healthy = False

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", **checks},
    )
