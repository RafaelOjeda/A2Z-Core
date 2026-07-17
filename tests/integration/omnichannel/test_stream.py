"""Integration tests for the SSE real-time relay (§5.4, Build Order Step 7).

Runs against fakeredis (the autouse ``fake_redis`` fixture in the top-level
conftest) -- real pub/sub, no mocking of the transport. The round-trip test
is the load-bearing one: it publishes through ``core.realtime.publish_update``
and receives through ``stream.stream_events``, locking the ``rt:{channel}``
key convention across the Core publish side and the service relay side so
either drifting fails here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.core import realtime
from app.main import app
from app.services.omnichannel import stream

pytestmark = pytest.mark.integration


@pytest.fixture
def client(aws: None) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


class _FakeRequest:
    """Minimal stand-in for a Starlette Request -- enough for the stream endpoint.

    Lets the happy-path endpoint test assert the ``StreamingResponse`` without
    a TestClient actually consuming the (unbounded) event stream.
    """

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}

    async def is_disconnected(self) -> bool:
        return False


async def _drain_until_data(
    gen: AsyncIterator[str], *, wait_seconds: float = 2.0
) -> tuple[list[str], str]:
    """Pull frames until the first ``data:`` frame; return (comments, data_frame)."""
    comments: list[str] = []

    async def _pull() -> str:
        async for frame in gen:
            if frame.startswith("data:"):
                return frame
            comments.append(frame)
        raise AssertionError("generator ended before a data frame")

    data_frame = await asyncio.wait_for(_pull(), timeout=wait_seconds)
    return comments, data_frame


async def test_publish_roundtrip_reaches_stream() -> None:
    """core.realtime.publish_update -> stream.stream_events, end to end."""
    gen = stream.stream_events("org-a", "agent-1", heartbeat_seconds=0.05)

    # First frame is the ``connected`` comment; pull it so the subscription
    # is definitely live before we publish.
    first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    assert first == ": connected\n\n"

    await realtime.publish_update("org-a", "org:org-a:conversations", {"type": "message.received"})

    _, data_frame = await _drain_until_data(gen)
    assert data_frame.startswith("data: ")
    assert data_frame.endswith("\n\n")
    payload = json.loads(data_frame[len("data: ") : -2])
    assert payload["type"] == "message.received"
    assert payload["org_id"] == "org-a"

    await gen.aclose()


async def test_user_notification_channel_is_subscribed() -> None:
    """A message on the agent's personal channel also reaches their stream."""
    gen = stream.stream_events("org-a", "agent-1", heartbeat_seconds=0.05)
    assert await asyncio.wait_for(gen.__anext__(), timeout=2.0) == ": connected\n\n"

    await realtime.publish_update(
        "org-a", "user:agent-1:notifications", {"type": "conversation.assigned"}
    )

    _, data_frame = await _drain_until_data(gen)
    payload = json.loads(data_frame[len("data: ") : -2])
    assert payload["type"] == "conversation.assigned"

    await gen.aclose()


async def test_cross_org_isolation() -> None:
    """An agent's stream never sees another org's inbox traffic."""
    gen = stream.stream_events("org-a", "agent-1", heartbeat_seconds=0.02)
    assert await asyncio.wait_for(gen.__anext__(), timeout=2.0) == ": connected\n\n"

    # Publish to a *different* org; agent-1's stream must not receive it.
    await realtime.publish_update("org-b", "org:org-b:conversations", {"type": "message.received"})

    # Give the loop time to tick a few heartbeats; assert no data frame shows up.
    frames: list[str] = []
    with pytest.raises(asyncio.TimeoutError):

        async def _collect() -> None:
            async for frame in gen:
                frames.append(frame)
                if frame.startswith("data:"):
                    return

        await asyncio.wait_for(_collect(), timeout=0.3)

    assert all(f.startswith(":") for f in frames)  # heartbeats/connected only
    await gen.aclose()


async def test_heartbeat_emitted_when_idle() -> None:
    """With no traffic, the stream emits keepalive comments on the heartbeat tick."""
    gen = stream.stream_events("org-a", "agent-1", heartbeat_seconds=0.02)
    assert await asyncio.wait_for(gen.__anext__(), timeout=2.0) == ": connected\n\n"
    assert await asyncio.wait_for(gen.__anext__(), timeout=2.0) == ": keepalive\n\n"
    await gen.aclose()


async def test_lifetime_cap_closes_stream() -> None:
    """The stream ends on its own once max_lifetime_seconds elapses."""
    fake_now = {"t": 1000.0}

    def _clock() -> float:
        return fake_now["t"]

    gen = stream.stream_events(
        "org-a",
        "agent-1",
        heartbeat_seconds=0.02,
        max_lifetime_seconds=5.0,
        clock=_clock,
    )
    assert await asyncio.wait_for(gen.__anext__(), timeout=2.0) == ": connected\n\n"
    # Advance the injected clock past the cap; the next tick should end it.
    fake_now["t"] = 1010.0

    remaining = [frame async for frame in gen]
    # No data frames; generator terminated rather than blocking forever.
    assert all(f.startswith(":") for f in remaining)


async def test_disconnect_predicate_closes_stream() -> None:
    """An is_disconnected() that flips True ends the stream promptly."""
    disconnected = {"v": False}

    async def _is_disconnected() -> bool:
        return disconnected["v"]

    gen = stream.stream_events(
        "org-a", "agent-1", heartbeat_seconds=0.02, is_disconnected=_is_disconnected
    )
    assert await asyncio.wait_for(gen.__anext__(), timeout=2.0) == ": connected\n\n"
    disconnected["v"] = True

    remaining = [frame async for frame in gen]
    assert all(f.startswith(":") for f in remaining)


# --- endpoint: auth + membership gating (§5.4) ---


def test_stream_endpoint_requires_token(client: TestClient) -> None:
    assert client.get("/omnichannel/orgs/org-a/stream").status_code == 401


def test_stream_endpoint_rejects_bad_token(client: TestClient) -> None:
    resp = client.get(
        "/omnichannel/orgs/org-a/stream", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert resp.status_code == 401


def test_stream_endpoint_non_member_is_404(
    client: TestClient, make_token: Callable[..., str]
) -> None:
    token = make_token("auth0|stranger", "stranger@example.com")
    resp = client.get(
        "/omnichannel/orgs/some-other-org/stream",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_stream_endpoint_member_returns_event_stream(
    aws: None, make_token: Callable[..., str]
) -> None:
    """A member gets a text/event-stream response (token via query param, the
    EventSource-friendly path). Called directly so the unbounded stream body
    is never consumed -- the relay behavior itself is covered above."""
    from app.core import membership
    from app.routers.omnichannel import stream_inbox

    token = make_token("auth0|owner", "owner@acme.com")
    org = await membership.create_org("Acme", "auth0|owner")

    resp = await stream_inbox(org.org_id, _FakeRequest(), access_token=token)  # type: ignore[arg-type]

    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "text/event-stream"
