"""Unit tests for core.events — verify the PutEvents call shape."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from app.core import clients, events
from app.core.exceptions import EventError


def _fake_eb(response: dict) -> Mock:
    eb = Mock()
    eb.put_events = Mock(return_value=response)
    return eb


async def test_publish_event_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    eb = _fake_eb({"FailedEntryCount": 0, "Entries": [{"EventId": "evt-123"}]})
    monkeypatch.setattr(clients, "eventbridge", lambda: eb)

    event_id = await events.publish_event(
        "acme", "member.added", {"user_id": "u1", "role": "member"}
    )

    assert event_id == "evt-123"
    entry = eb.put_events.call_args.kwargs["Entries"][0]
    assert entry["Source"] == "a2z.core"
    assert entry["DetailType"] == "member.added"
    assert entry["EventBusName"] == "a2z-bus"
    detail = json.loads(entry["Detail"])
    assert detail["org_id"] == "acme"  # org_id always injected
    assert detail["user_id"] == "u1"


async def test_custom_source(monkeypatch: pytest.MonkeyPatch) -> None:
    eb = _fake_eb({"FailedEntryCount": 0, "Entries": [{"EventId": "x"}]})
    monkeypatch.setattr(clients, "eventbridge", lambda: eb)
    await events.publish_event("o", "invoice.paid", {}, source="a2z.invoicing")
    assert eb.put_events.call_args.kwargs["Entries"][0]["Source"] == "a2z.invoicing"


async def test_failed_entry_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    eb = _fake_eb(
        {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "X", "ErrorMessage": "boom"}]}
    )
    monkeypatch.setattr(clients, "eventbridge", lambda: eb)
    with pytest.raises(EventError):
        await events.publish_event("o", "member.added", {})


@pytest.mark.integration
async def test_publish_against_moto(aws: None) -> None:
    event_id = await events.publish_event("org-x", "member.removed", {"user_id": "u"})
    assert event_id  # moto returns a real-looking EventId
