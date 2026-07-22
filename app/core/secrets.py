"""Secrets — per-org, per-service credential access (root CLAUDE.md §6.2 gap).

Backed by AWS Secrets Manager; cached in Redis (5-min TTL) using the same
idiom ``core.settings`` already uses for org config, rather than the
sync-only ``aws-secretsmanager-caching`` client the original Omni-Channel
plan called for (⚠ ADAPTED — see app/services/omnichannel/CLAUDE.md §6.2).

Secret name convention: ``a2z/{org_id}/{service_type}/{key}`` — e.g. a
WhatsApp Business token for one org's Omni-Channel connection. Secret values
are never logged; only ``org_id``, ``service_type``, ``key``, and cache
hit/miss.

``put_secret`` is the self-service write path (2026-07-18 addition): a
service's connect-a-channel flow calls it directly with credentials a user
just typed in, instead of requiring an engineer to write the value into
Secrets Manager by hand first. It invalidates the Redis cache key on write
so a read immediately after a write is never stale; a read that raced the
write and already cached the old (or absent) value can still lag by up to
the 5-minute TTL, same accepted window as before.
"""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError

from app.core import clients
from app.core.exceptions import SecretNotFoundError, SecretsError
from app.core.logging import get_logger

log = get_logger("core.secrets")

_CACHE_TTL_SECONDS = 300  # 5 min, matches core.settings


def _secret_name(org_id: str, service_type: str, key: str) -> str:
    return f"a2z/{org_id}/{service_type}/{key}"


def _cache_key(org_id: str, service_type: str, key: str) -> str:
    return f"secret:{org_id}:{service_type}:{key}"


async def get_secret(org_id: str, service_type: str, key: str) -> dict[str, Any]:
    """Fetch a secret for an org/service pair (e.g. a WhatsApp access token).

    Args:
        org_id: Org the secret belongs to (always required — no cross-org reads).
        service_type: Owning service, e.g. ``"omnichannel"``.
        key: Logical name within that org/service, e.g. ``"whatsapp"``.

    Returns:
        The secret value, parsed as a JSON object.

    Raises:
        SecretNotFoundError: No secret exists at that org/service/key.

    Performance: < 20ms on a cache hit, < 200ms on a cache miss.
    """
    redis = clients.redis_client()
    cache_key = _cache_key(org_id, service_type, key)
    cached = await redis.get(cache_key)
    if cached is not None:
        log.info(
            "secret.cache_hit",
            extra={"org_id": org_id, "service_type": service_type, "key": key},
        )
        result: dict[str, Any] = json.loads(cached)
        return result

    name = _secret_name(org_id, service_type, key)
    try:
        resp = await clients.run_aws(clients.secretsmanager().get_secret_value, SecretId=name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "InvalidRequestException"):
            raise SecretNotFoundError(f"No secret at {name}") from exc
        raise

    raw = resp["SecretString"]
    value: dict[str, Any] = json.loads(raw)
    await redis.set(cache_key, raw, ex=_CACHE_TTL_SECONDS)
    log.info(
        "secret.cache_miss",
        extra={"org_id": org_id, "service_type": service_type, "key": key},
    )
    return value


async def put_secret(org_id: str, service_type: str, key: str, value: dict[str, Any]) -> None:
    """Create or update a secret for an org/service pair.

    The self-service counterpart to :func:`get_secret`: a connect-a-channel
    flow calls this with credentials a user just submitted (e.g. a WhatsApp
    access token pasted into a form), so no one needs AWS console/CLI access
    to onboard an org.

    Args:
        org_id: Org the secret belongs to (always required — no cross-org writes).
        service_type: Owning service, e.g. ``"omnichannel"``.
        key: Logical name within that org/service, e.g. a connection id.
        value: The secret value; stored as JSON. Never logged.

    Raises:
        SecretsError: The underlying Secrets Manager call failed.

    Performance: < 300ms (uncached — one or two Secrets Manager calls).
    """
    name = _secret_name(org_id, service_type, key)
    raw = json.dumps(value)
    try:
        await clients.run_aws(
            clients.secretsmanager().put_secret_value, SecretId=name, SecretString=raw
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "ResourceNotFoundException":
            raise SecretsError(f"Failed to write secret at {name}: {exc}") from exc
        try:
            await clients.run_aws(
                clients.secretsmanager().create_secret, Name=name, SecretString=raw
            )
        except ClientError as create_exc:
            raise SecretsError(f"Failed to create secret at {name}: {create_exc}") from create_exc

    # Invalidate rather than write-through: the next get_secret repopulates
    # from the value we just wrote, so this can't drift from what's in AWS.
    await clients.redis_client().delete(_cache_key(org_id, service_type, key))
    log.info(
        "secret.write",
        extra={"org_id": org_id, "service_type": service_type, "key": key},
    )
