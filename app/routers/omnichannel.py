"""Thin HTTP layer for Omni-Channel (root CLAUDE.md §2: routers are thin).

Business logic lives in ``app.services.omnichannel.*``; this module only
parses the request and calls into it. Errors are typed ``CoreError``
subclasses, mapped to HTTP responses by the global handler in ``app.main``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import auth, membership
from app.core.exceptions import NotFoundError
from app.dependencies import CurrentUser
from app.services.omnichannel import connections, handlers, inbox, routing, stream
from app.services.omnichannel.db import get_session
from app.services.omnichannel.webhooks import handle_webhook
from app.services.omnichannel.webhooks import verify_subscription as webhook_verify_subscription

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


@router.get("/webhooks/{channel_type}/{connection_id}", response_class=PlainTextResponse)
async def verify_webhook_subscription(
    channel_type: str,
    connection_id: str,
    request: Request,
    session: DbSession,
) -> PlainTextResponse:
    """Answer a provider's webhook-subscription verification handshake (§5.6).

    Meta's WhatsApp Cloud API calls this once when a webhook URL is
    registered in the App dashboard, before it will ever deliver a real
    ``POST``. No bearer auth: same reasoning as the ``POST`` route --
    authenticity here is the channel's own verify-token check
    (``webhooks.verify_subscription``), not a JWT. **No dedicated response
    model**: providers expect the raw challenge string back as
    ``text/plain``, not a JSON envelope.
    """
    params = dict(request.query_params)
    challenge = await webhook_verify_subscription(session, channel_type, connection_id, params)
    return PlainTextResponse(challenge)


@router.get("/orgs/{org_id}/conversations")
async def list_conversations(
    org_id: str,
    user: CurrentUser,
    session: DbSession,
    status: str | None = None,
    assigned_user_id: str | None = None,
    q: str | None = None,
    sort: str = "-last_message_at",
    limit: int = inbox.DEFAULT_CONVERSATION_LIMIT,
    cursor: str | None = None,
) -> inbox.ConversationPage:
    """The unified inbox: an org's conversations, most recently active first (§3).

    Cursor pagination: pass a previous page's ``next_cursor`` back as
    ``cursor`` to fetch the next page; ``next_cursor: null`` means there
    isn't one. ``q`` searches customer name and message text; ``sort`` is
    ``-last_message_at`` (default) or ``last_message_at``.
    """
    return await inbox.list_conversations(
        session,
        org_id,
        user["sub"],
        status=status,
        assigned_user_id=assigned_user_id,
        q=q,
        sort=sort,
        limit=limit,
        cursor=cursor,
    )


@router.get("/orgs/{org_id}/conversations/{conversation_id}")
async def get_conversation(
    org_id: str,
    conversation_id: str,
    user: CurrentUser,
    session: DbSession,
    limit: int = inbox.DEFAULT_MESSAGE_LIMIT,
    before: str | None = None,
) -> inbox.ConversationDetail:
    """One conversation's thread, with signed attachment URLs (§3, §10).

    ``before`` (a previous response's ``messages_next_cursor``) fetches the
    next-older page of messages, for scrolling back through a long thread.
    """
    return await inbox.get_conversation(
        session, org_id, conversation_id, user["sub"], limit=limit, before=before
    )


class MarkReadResponse(BaseModel):
    conversation_id: str
    unread_count: int


@router.post("/orgs/{org_id}/conversations/{conversation_id}/read")
async def mark_read(
    org_id: str,
    conversation_id: str,
    user: CurrentUser,
    session: DbSession,
) -> MarkReadResponse:
    """Clear a conversation's unread badge. POST, not a side effect of the GET."""
    conversation = await inbox.mark_read(session, org_id, conversation_id, user["sub"])
    return MarkReadResponse(conversation_id=conversation.id, unread_count=conversation.unread_count)


class SendReplyRequest(BaseModel):
    body_text: str


class SendReplyResponse(BaseModel):
    message_id: str
    status: str


@router.post("/orgs/{org_id}/conversations/{conversation_id}/messages", status_code=201)
async def send_reply(
    org_id: str,
    conversation_id: str,
    body: SendReplyRequest,
    user: CurrentUser,
    session: DbSession,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SendReplyResponse:
    """Send an agent's reply in a conversation -- the outbound half of §5.6.

    ``Idempotency-Key`` (optional): a retried request with the same key
    returns the original message instead of sending a duplicate, and the
    response status drops to 200 (a replay, not a new creation) -- the
    default 201 is reserved for an actual new message.
    """
    message, created = await handlers.send_reply(
        session,
        org_id,
        conversation_id,
        user["sub"],
        body.body_text,
        client_dedup_key=idempotency_key,
    )
    if not created:
        response.status_code = 200
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


class RoutingConfigResponse(BaseModel):
    routing_strategy: str
    single_assignee_user_id: str | None


@router.put("/orgs/{org_id}/routing-config")
async def set_routing_config(
    org_id: str,
    body: RoutingConfigRequest,
    user: CurrentUser,
) -> RoutingConfigResponse:
    """Owner/Admin sets the org's routing strategy (§5.3, §4)."""
    config = await routing.set_routing_config(
        org_id, user["sub"], body.strategy, body.single_assignee_user_id
    )
    return RoutingConfigResponse(**config)


class ConnectionCreateRequest(BaseModel):
    channel_type: str
    display_name: str
    provider_account_id: str
    credentials_secret_key: str


@router.post("/orgs/{org_id}/connections", status_code=201)
async def create_connection(
    org_id: str,
    body: ConnectionCreateRequest,
    user: CurrentUser,
    session: DbSession,
) -> connections.ConnectionView:
    """Register a channel connection for an org (Owner/Admin only, §5.2, §5.6)."""
    connection = await connections.create_connection(
        session,
        org_id,
        user["sub"],
        channel_type=body.channel_type,
        display_name=body.display_name,
        provider_account_id=body.provider_account_id,
        credentials_secret_key=body.credentials_secret_key,
    )
    return connections.to_view(connection)


@router.get("/orgs/{org_id}/connections")
async def list_connections(
    org_id: str,
    user: CurrentUser,
    session: DbSession,
) -> connections.ConnectionPage:
    """List an org's channel connections (Owner/Admin only)."""
    rows = await connections.list_connections(session, org_id, user["sub"])
    return connections.ConnectionPage(items=[connections.to_view(c) for c in rows])


@router.get("/orgs/{org_id}/connections/{connection_id}")
async def get_connection(
    org_id: str,
    connection_id: str,
    user: CurrentUser,
    session: DbSession,
) -> connections.ConnectionView:
    """Read one channel connection (Owner/Admin only)."""
    connection = await connections.get_connection(session, org_id, user["sub"], connection_id)
    return connections.to_view(connection)


@router.delete("/orgs/{org_id}/connections/{connection_id}", status_code=204)
async def disable_connection(
    org_id: str,
    connection_id: str,
    user: CurrentUser,
    session: DbSession,
) -> None:
    """Soft-disable a channel connection (Owner/Admin only) -- its inbound
    webhooks are rejected from this point on (§5.6)."""
    await connections.disable_connection(session, org_id, user["sub"], connection_id)


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
