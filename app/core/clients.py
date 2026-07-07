"""boto3 + Redis client factories (module-level singletons).

This is the *only* place Core builds AWS / Redis clients (CLAUDE.md §4):

  * boto3 clients are sync; we build them once and reuse. Hot-path Core
    functions must never construct a client. To keep the spec's ``async def``
    signatures non-blocking, wrap each sync AWS call in :func:`run_aws`, which
    offloads to a thread (``asyncio.to_thread``).
  * Redis uses the native async client (``redis.asyncio``) with a shared
    connection pool.

Endpoint URLs come from config so LocalStack can override every service via
``AWS_ENDPOINT_URL`` (CLAUDE.md §12). Credentials come from the ECS task IAM
role in AWS; LocalStack accepts the dummy ``test`` creds from ``.env``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

import boto3
import redis.asyncio as aioredis
from botocore.config import Config as BotoConfig

from app.config import settings

if TYPE_CHECKING:  # import only for type checkers; avoids runtime cost
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_events import EventBridgeClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_ses import SESClient
    from mypy_boto3_sns import SNSClient

# Modest, bounded retries keep tail latency predictable instead of hanging.
_BOTO_CONFIG = BotoConfig(
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=3,
    read_timeout=5,
)


def _client(service: str) -> Any:
    # boto3-stubs only types literal service names; we pass a runtime string and
    # each caller casts the result to the correct typed client.
    s = settings()
    return boto3.client(  # type: ignore[call-overload]
        service,
        region_name=s.aws_region,
        endpoint_url=s.aws_endpoint_url or None,
        config=_BOTO_CONFIG,
    )


@lru_cache(maxsize=1)
def dynamodb() -> DynamoDBClient:
    return cast("DynamoDBClient", _client("dynamodb"))


@lru_cache(maxsize=1)
def s3() -> S3Client:
    return cast("S3Client", _client("s3"))


@lru_cache(maxsize=1)
def ses() -> SESClient:
    return cast("SESClient", _client("ses"))


@lru_cache(maxsize=1)
def sns() -> SNSClient:
    return cast("SNSClient", _client("sns"))


@lru_cache(maxsize=1)
def eventbridge() -> EventBridgeClient:
    return cast("EventBridgeClient", _client("events"))


@lru_cache(maxsize=1)
def redis_client() -> aioredis.Redis[str]:
    """Shared async Redis client (decodes responses to str)."""
    return aioredis.from_url(settings().redis_url, decode_responses=True)


async def run_aws[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a sync boto3 call in a worker thread so it doesn't block the loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def reset_clients() -> None:
    """Clear cached clients. Used by tests after pointing at a fresh backend."""
    for factory in (dynamodb, s3, ses, sns, eventbridge, redis_client):
        # redis_client may be monkeypatched in tests (no lru_cache wrapper).
        clear = getattr(factory, "cache_clear", None)
        if clear is not None:
            clear()
