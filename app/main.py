"""FastAPI entrypoint for the A2Z modular monolith.

Mounts the thin Core routers and maps the typed ``CoreError`` hierarchy onto HTTP
responses (each error carries its own ``status_code``). Services mount their own
routers here in later phases.

**Versioning (API review, 2026-07-18):** every router except ``health`` is
mounted under ``/v1`` -- there was previously no version anywhere in the
surface, so a future breaking change had nowhere to go without shifting
every existing path out from under already-integrated callers. ``/health``
stays unversioned: it's an infra liveness probe, not a client-facing
contract, and load balancers/ECS health checks shouldn't need updating on a
version bump. Policy: additive changes (new fields, new endpoints) land in
place under ``/v1``; a breaking change to an existing endpoint's shape mints
``/v2`` for that router rather than mutating ``/v1`` out from under callers
(see ``docs/api-reference.md``).
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.exceptions import CoreError, RateLimitError
from app.core.logging import get_logger, request_id_var
from app.routers import core_admin, health, omnichannel, invoicing

log = get_logger("app.main")

app = FastAPI(title="A2Z Core", version="0.1.0")
app.include_router(health.router)
app.include_router(core_admin.router, prefix="/v1")
app.include_router(omnichannel.router, prefix="/v1")
app.include_router(invoicing.router)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Thread a request id through logs and echo it back to the client."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["x-request-id"] = rid
    return response


@app.exception_handler(CoreError)
async def core_error_handler(request: Request, exc: CoreError) -> JSONResponse:
    """Map any CoreError to its status_code; set Retry-After for rate limits."""
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimitError):
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc), "error": type(exc).__name__},
        headers=headers,
    )
