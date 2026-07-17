"""Thin HTTP layer for Omni-Channel (root CLAUDE.md §2: routers are thin).

Business logic lives in ``app.services.omnichannel.*``; this module only
parses the request and calls into it. Errors are typed ``CoreError``
subclasses, mapped to HTTP responses by the global handler in ``app.main``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import auth, membership
from app.core.exceptions import NotFoundError
from app.dependencies import CurrentUser
from app.services.omnichannel import handlers, inbox, routing, stream
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


@router.get("/orgs/{org_id}/conversations")
async def list_conversations(
    org_id: str,
    user: CurrentUser,
    session: DbSession,
    status: str | None = None,
    assigned_user_id: str | None = None,
    limit: int = inbox.DEFAULT_CONVERSATION_LIMIT,
    offset: int = 0,
) -> list[inbox.ConversationSummary]:
    """The unified inbox: an org's conversations, most recently active first (§3)."""
    return await inbox.list_conversations(
        session,
        org_id,
        user["sub"],
        status=status,
        assigned_user_id=assigned_user_id,
        limit=limit,
        offset=offset,
    )


@router.get("/orgs/{org_id}/conversations/{conversation_id}")
async def get_conversation(
    org_id: str,
    conversation_id: str,
    user: CurrentUser,
    session: DbSession,
    limit: int = inbox.DEFAULT_MESSAGE_LIMIT,
) -> inbox.ConversationDetail:
    """One conversation's thread, with signed attachment URLs (§3, §10)."""
    return await inbox.get_conversation(session, org_id, conversation_id, user["sub"], limit=limit)


@router.post("/orgs/{org_id}/conversations/{conversation_id}/read")
async def mark_read(
    org_id: str,
    conversation_id: str,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, int | str]:
    """Clear a conversation's unread badge. POST, not a side effect of the GET."""
    conversation = await inbox.mark_read(session, org_id, conversation_id, user["sub"])
    return {"conversation_id": conversation.id, "unread_count": conversation.unread_count}


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


class AssignmentResponse(BaseModel):
    conversation_id: str
    assigned_user_id: str | None


@router.post("/orgs/{org_id}/conversations/{conversation_id}/claim")
async def claim_conversation(
    org_id: str,
    conversation_id: str,
    user: CurrentUser,
    session: DbSession,
) -> AssignmentResponse:
    """An agent claims an unassigned conversation (§5.3, §4)."""
    conversation = await routing.claim(session, org_id, conversation_id, user["sub"])
    return AssignmentResponse(
        conversation_id=conversation.id, assigned_user_id=conversation.assigned_user_id
    )


class ReassignRequest(BaseModel):
    assignee_user_id: str


@router.post("/orgs/{org_id}/conversations/{conversation_id}/reassign")
async def reassign_conversation(
    org_id: str,
    conversation_id: str,
    body: ReassignRequest,
    user: CurrentUser,
    session: DbSession,
) -> AssignmentResponse:
    """Owner/Admin reassigns a conversation to a different member (§5.3, §4)."""
    conversation = await routing.reassign(
        session, org_id, conversation_id, user["sub"], body.assignee_user_id
    )
    return AssignmentResponse(
        conversation_id=conversation.id, assigned_user_id=conversation.assigned_user_id
    )


class RoutingConfigRequest(BaseModel):
    strategy: str
    single_assignee_user_id: str | None = None


@router.put("/orgs/{org_id}/routing-config")
async def set_routing_config(
    org_id: str,
    body: RoutingConfigRequest,
    user: CurrentUser,
) -> dict[str, Any]:
    """Owner/Admin sets the org's routing strategy (§5.3, §4)."""
    return await routing.set_routing_config(
        org_id, user["sub"], body.strategy, body.single_assignee_user_id
    )


@router.get("/orgs/{org_id}/stream")
async def stream_inbox(
    org_id: str,
    request: Request,
    access_token: str | None = None,
) -> StreamingResponse:
    """Live inbox updates over Server-Sent Events (§5.4).

    Auth is handled inline rather than via the shared ``CurrentUser``
    dependency because a browser ``EventSource`` cannot set an
    ``Authorization`` header -- so the token may arrive as the
    ``access_token`` query param instead, with the header still accepted as
    a fallback (e.g. curl / a proxied client). Membership is re-checked here
    on every (re)connect, so a revoked member's stream ends on their next
    reconnect (§5.4).
    """
    token = access_token
    if token is None:
        header = request.headers.get("authorization") or request.headers.get("Authorization")
        if header and header.lower().startswith("bearer "):
            token = header.split(" ", 1)[1].strip()
    claims = auth.validate_jwt(token or "")
    user_id = claims["sub"]

    if await membership.get_membership(user_id, org_id) is None:
        raise NotFoundError("Not a member of this org")

    return StreamingResponse(
        stream.stream_events(org_id, user_id, is_disconnected=request.is_disconnected),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering so events flush immediately
        },
    )
