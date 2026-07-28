"""boto3 client factories (module-level singletons).

This is the *only* place Core builds AWS clients (CLAUDE.md §4): boto3
clients are sync; we build them once and reuse. Hot-path Core functions must
never construct a client. To keep the spec's ``async def`` signatures
non-blocking, wrap each sync AWS call in :func:`run_aws`, which offloads to a
thread (``asyncio.to_thread``).

Endpoint URLs come from config so LocalStack can override every service via
``AWS_ENDPOINT_URL`` (CLAUDE.md §12). Credentials come from the ECS task IAM
role in AWS; LocalStack accepts the dummy ``test`` creds from ``.env``.

(Redis is gone -- single-box MVP, docs/architecture/single-box-mvp.md --
so there is no longer a Redis factory here; see ``core.realtime``,
``core.rate_limit``, and ``core.cache`` for what replaced it.)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

import boto3
import httpx
from botocore.config import Config as BotoConfig

from app.config import settings

if TYPE_CHECKING:  # import only for type checkers; avoids runtime cost
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_events import EventBridgeClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_secretsmanager import SecretsManagerClient
    from mypy_boto3_ses import SESClient
    from mypy_boto3_sns import SNSClient
    from mypy_boto3_sqs import SQSClient

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
def secretsmanager() -> SecretsManagerClient:
    return cast("SecretsManagerClient", _client("secretsmanager"))


@lru_cache(maxsize=1)
def sqs() -> SQSClient:
    # Service-owned queueing (Omni-Channel's shared inbound/outbound queues,
    # app/services/omnichannel/CLAUDE.md §5.6/§12) -- lives here per this
    # module's own rule: the only place boto3 clients are built.
    return cast("SQSClient", _client("sqs"))


@lru_cache(maxsize=1)
def http_client() -> httpx.AsyncClient:
    """Shared async HTTP client for outbound calls to third-party channel
    providers (WhatsApp Graph API, etc. — Omni-Channel's adapters)."""
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))


async def run_aws[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a sync boto3 call in a worker thread so it doesn't block the loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def reset_clients() -> None:
    """Clear cached clients. Used by tests after pointing at a fresh backend."""
    for factory in (dynamodb, s3, ses, sns, eventbridge, secretsmanager, sqs):
        factory.cache_clear()
