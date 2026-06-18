"""Health endpoint — checks DynamoDB + Redis reachability (DoD, CLAUDE.md §15)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core import clients
from app.core.logging import get_logger

router = APIRouter(tags=["health"])
log = get_logger("router.health")


@router.get("/health")
async def health() -> JSONResponse:
    """Liveness/readiness: 200 when DynamoDB and Redis are reachable, else 503."""
    checks: dict[str, str] = {}
    healthy = True

    try:
        await clients.run_aws(clients.dynamodb().list_tables)
        checks["dynamodb"] = "ok"
    except Exception:  # noqa: BLE001 — health probe must not raise
        checks["dynamodb"] = "error"
        healthy = False

    try:
        await clients.redis_client().ping()
        checks["redis"] = "ok"
    except Exception:  # noqa: BLE001
        checks["redis"] = "error"
        healthy = False

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", **checks},
    )
