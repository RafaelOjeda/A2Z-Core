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

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.exceptions import CoreError, RateLimitError
from app.core.logging import get_logger, request_id_var
from app.routers import core_admin, health, invoicing, landing, omnichannel, ses_notifications
from app.services.omnichannel import worker as omnichannel_worker

log = get_logger("app.main")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Start/stop the Omni-Channel worker loop alongside the API process.

    Single-box MVP (docs/architecture/single-box-mvp.md): there is no
    separate worker process, so the SQS-draining loop
    (``omnichannel.worker.run_forever``) runs as a background task in this
    same event loop. Gated on ``settings().run_omnichannel_worker``
    (default OFF) so every test that boots the app via ``TestClient`` -- most
    of the integration suite -- never races a live worker against moto's SQS
    state; the real deployment turns it on via env (Dockerfile/user-data.sh).
    """
    task: asyncio.Task[None] | None = None
    if settings().run_omnichannel_worker:
        task = asyncio.create_task(omnichannel_worker.run_forever())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="A2Z Core", version="0.1.0", lifespan=lifespan)
app.include_router(landing.router)
app.include_router(health.router)
app.include_router(ses_notifications.router)
app.include_router(core_admin.router, prefix="/v1")
app.include_router(omnichannel.router, prefix="/v1")
app.include_router(invoicing.router, prefix="/v1")


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
