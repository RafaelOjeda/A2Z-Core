"""Thin HTTP layer for Omni-Channel (root CLAUDE.md §2: routers are thin).

Business logic lives in ``app.services.omnichannel.*``; this module only
parses the request and calls into it. Errors are typed ``CoreError``
subclasses, mapped to HTTP responses by the global handler in ``app.main``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.services.omnichannel import handlers
from app.services.omnichannel.db import get_session
from app.services.omnichannel.webhooks import handle_webhook

router = APIRouter(prefix="/omnichannel", tags=["omnichannel"])

DbSession = Annotated[AsyncSession, Depends(get_session)]


@router.post("/webhooks/{channel_type}/{connection_id}")
async def receive_webhook(
    channel_type: str,
    connection_id: str,
    request: Request,
    session: DbSession,
) -> dict[str, str]:
    """Generic inbound webhook route -- one route for every channel (§5.6)."""
    raw_body = await request.body()
    headers = dict(request.headers)
    await handle_webhook(session, channel_type, connection_id, raw_body, headers)
    return {"status": "accepted"}


class SendReplyRequest(BaseModel):
    body_text: str


class SendReplyResponse(BaseModel):
    message_id: str
    status: str


@router.post("/orgs/{org_id}/conversations/{conversation_id}/messages")
async def send_reply(
    org_id: str,
    conversation_id: str,
    body: SendReplyRequest,
    user: CurrentUser,
    session: DbSession,
) -> SendReplyResponse:
    """Send an agent's reply in a conversation -- the outbound half of §5.6."""
    message = await handlers.send_reply(
        session, org_id, conversation_id, user["sub"], body.body_text
    )
    return SendReplyResponse(message_id=message.id, status=message.status)
