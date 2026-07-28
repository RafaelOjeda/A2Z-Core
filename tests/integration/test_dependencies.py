"""Integration tests for app.dependencies.current_user (moto).

Covers the user-provisioning behavior that used to live in the Cognito
post-confirm Lambda (``app/lambdas/cognito_post_confirm.py``, removed on the
single-box MVP -- docs/architecture/single-box-mvp.md). CLAUDE.md §5 always
preferred bootstrapping on the first authenticated request over the Lambda;
this is that preference actually implemented, since there's no Lambda left
to do it instead.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import settings as app_settings
from app.core import auth, clients
from app.core._ddb import from_item, to_item
from app.dependencies import _provisioned, current_user

pytestmark = pytest.mark.integration


class _FakeRequest:
    """Minimal stand-in for a Starlette Request -- only `.headers` is used."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _auth_headers(sub: str, email: str) -> dict[str, str]:
    token = auth.create_test_token(sub, email)
    return {"Authorization": f"Bearer {token}"}


async def _get_user_item(sub: str) -> dict[str, Any] | None:
    resp = await clients.run_aws(
        clients.dynamodb().get_item,
        TableName=app_settings().tables["membership"],
        Key=to_item({"PK": f"USER#{sub}", "SK": "METADATA"}),
    )
    return from_item(resp["Item"]) if resp.get("Item") else None


async def test_first_authenticated_request_provisions_the_user(aws: None) -> None:
    request = _FakeRequest(_auth_headers("auth0|new-user", "new@example.com"))

    claims = await current_user(request)  # type: ignore[arg-type]

    assert claims["sub"] == "auth0|new-user"
    item = await _get_user_item("auth0|new-user")
    assert item is not None
    assert item["email"] == "new@example.com"


async def test_second_request_does_not_reprovision(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import membership

    request = _FakeRequest(_auth_headers("auth0|repeat-user", "repeat@example.com"))
    await current_user(request)  # type: ignore[arg-type]
    assert "auth0|repeat-user" in _provisioned

    calls = 0
    real_create = membership.create_user_if_not_exists

    async def _counting_create(user_id: str, email: str) -> None:
        nonlocal calls
        calls += 1
        await real_create(user_id, email)

    monkeypatch.setattr(membership, "create_user_if_not_exists", _counting_create)

    await current_user(request)  # type: ignore[arg-type]
    assert calls == 0  # the in-process cache skipped the second attempt entirely


async def test_missing_email_claim_does_not_crash(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Cognito tokens always carry an email claim, but the defensive
    ``if sub and email`` check must hold even if one ever didn't -- no
    provisioning attempt, no crash."""
    from app.core import membership

    monkeypatch.setattr(
        auth, "get_current_user_from_request", lambda request: {"sub": "auth0|no-email"}
    )
    calls = 0

    async def _counting_create(user_id: str, email: str) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(membership, "create_user_if_not_exists", _counting_create)

    claims = await current_user(_FakeRequest({}))  # type: ignore[arg-type]
    assert claims["sub"] == "auth0|no-email"
    assert calls == 0
