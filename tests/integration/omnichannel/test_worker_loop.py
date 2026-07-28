"""Integration tests for ``worker.run_forever`` (single-box MVP lifespan task).

Until this loop existed, nothing drained the inbound/outbound SQS queues in
the deployed artifact -- ``process_inbound_batch``/``process_outbound_batch``
were called only by tests, never by a running process (see run_forever's
module docstring). These tests exercise the loop itself rather than the
batch functions directly.

``test_worker_loop_delivers_to_a_live_sse_subscriber`` is the load-bearing
one: it proves the loop, running as a background task the way
``app.main``'s lifespan starts it, actually crosses the process-internal
boundary between "the worker persists a message and calls
``core.realtime.publish_update``" and "an open SSE stream (a different
coroutine, standing in for a different request/connection) receives it".
That boundary is exactly what an in-process broker would fail to bridge if
the worker still ran as a separate OS process -- which is why collapsing it
into the API process (rather than building the missing second process) was
the right call, not just the cheap one.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clients, realtime
from app.services.omnichannel import webhooks, worker
from app.services.omnichannel.models import ChannelConnection

pytestmark = pytest.mark.integration

_APP_SECRET = "wa-app-secret"


def _sign(raw_body: bytes) -> str:
    mac = hmac.new(_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _whatsapp_payload(from_number: str = "15551234567", text: str = "Hi") -> bytes:
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": from_number,
                                        "id": "wamid.LOOP-1",
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ],
                                "contacts": [{"wa_id": from_number, "profile": {"name": "Jane"}}],
                            }
                        }
                    ]
                }
            ]
        }
    ).encode("utf-8")


async def _seed_connection(session: AsyncSession, org_id: str = "org-loop") -> ChannelConnection:
    connection = ChannelConnection(
        org_id=org_id,
        channel_type="whatsapp",
        display_name="Test WhatsApp",
        provider_account_id="15550001111",
        credentials_secret_key="whatsapp-main",
        status="active",
    )
    session.add(connection)
    await session.commit()
    return connection


async def _seed_secret(org_id: str, key: str, value: dict[str, str]) -> None:
    await clients.run_aws(
        clients.secretsmanager().create_secret,
        Name=f"a2z/{org_id}/omnichannel/{key}",
        SecretString=json.dumps(value),
    )


async def test_worker_loop_starts_runs_and_stops_cleanly(aws: None) -> None:
    """One iteration, then cancel -- run_forever must exit on CancelledError
    without raising anything else, and must not busy-loop (a short
    poll_interval on empty queues should still yield control)."""
    task = asyncio.create_task(worker.run_forever(poll_interval=0.01, wait_time_seconds=0))
    await asyncio.sleep(0.1)  # let it run a few empty iterations
    assert not task.done()  # still looping, not crashed

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_worker_loop_survives_a_bad_iteration(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single iteration's exception must not kill the loop (run_forever's
    "must never die" contract) -- it logs and continues."""
    calls = 0
    real_process_inbound = worker.process_inbound_batch

    async def _flaky(session: AsyncSession, **kwargs: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated transient failure")
        return await real_process_inbound(session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker, "process_inbound_batch", _flaky)

    task = asyncio.create_task(worker.run_forever(poll_interval=0.01, wait_time_seconds=0))
    await asyncio.sleep(0.1)
    assert calls >= 2  # the loop kept going past the simulated failure
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_worker_loop_delivers_to_a_live_sse_subscriber(
    aws: None, session: AsyncSession
) -> None:
    """The single most important test in this file -- see module docstring."""
    connection = await _seed_connection(session)
    await _seed_secret(
        connection.org_id, connection.credentials_secret_key, {"app_secret": _APP_SECRET}
    )

    # Stand in for an agent's already-open SSE connection (stream.py subscribes
    # to exactly this logical channel -- see stream._channels_for).
    async with realtime.subscribe(f"org:{connection.org_id}:conversations") as sub:
        raw_body = _whatsapp_payload()
        headers = {"X-Hub-Signature-256": _sign(raw_body)}
        await webhooks.handle_webhook(session, "whatsapp", connection.id, raw_body, headers)

        task = asyncio.create_task(worker.run_forever(poll_interval=0.01, wait_time_seconds=0))
        try:
            message = await sub.get(timeout_seconds=5.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert message is not None
    payload = json.loads(message)
    assert payload["org_id"] == connection.org_id
    assert payload["type"] == "message.received"
