"""Cached signed URLs for message attachments -- the §10 S3-egress mitigation.

Why cache a *signed URL* when signing is local and free? Because
``generate_presigned_url`` embeds a fresh signature (and timestamp) every
call, so re-signing the same object hands the browser a brand-new URL each
time -- which busts its HTTP cache and forces a re-download of bytes it
already has. Handing back a stable URL for the object's cache window is what
actually saves the egress (§10).

Two invariants:

1. **Cache TTL < signed expiry.** The URL is signed for
   ``_SIGN_EXPIRY_SECONDS`` but cached for only ``_CACHE_TTL_SECONDS``, so a
   URL served from cache always has at least
   (expiry - TTL) of validity left. Cache-for-exactly-the-signed-lifetime
   would let a cache hit return a URL that expires seconds later.
2. **Org scope is enforced here.** ``core.storage.generate_signed_url`` takes
   no ``org_id`` and does *not* check the key prefix (unlike
   ``download_file``/``get_file_metadata``, which call ``_assert_org_scope``)
   -- it is a pure local signing call. So this module does the check itself
   before signing: without it, a caller could mint a working URL for another
   org's object, which golden rule #2 forbids.
"""

from __future__ import annotations

from app.core import clients
from app.core.exceptions import StorageError
from app.core.logging import get_logger
from app.core.storage import generate_signed_url

log = get_logger("omnichannel.media")

_SIGN_EXPIRY_SECONDS = 2 * 3600  # 2h -- must exceed the cache TTL below
_CACHE_TTL_SECONDS = 3600  # 1h (§10)


def _cache_key(s3_key: str) -> str:
    return f"mediaurl:{s3_key}"


async def signed_url_for_attachment(org_id: str, s3_key: str) -> str:
    """Return a cached, org-scoped signed GET URL for an attachment.

    Args:
        org_id: Org requesting the URL. The key must live under this org's
            prefix -- there is no cross-org read path.
        s3_key: The attachment's S3 key (``{org_id}/omnichannel/...``).

    Returns:
        A presigned GET URL, valid for at least
        ``_SIGN_EXPIRY_SECONDS - _CACHE_TTL_SECONDS`` from now.

    Raises:
        StorageError: ``s3_key`` does not belong to ``org_id``.

    Performance: < 10ms on a cache hit; < 50ms on a miss (local signing).
    """
    if not s3_key.startswith(f"{org_id}/"):
        # Mirrors core.storage._assert_org_scope, which generate_signed_url
        # itself skips. Never mint a URL for another org's object.
        raise StorageError("File does not belong to this org")

    redis = clients.redis_client()
    cache_key = _cache_key(s3_key)
    cached = await redis.get(cache_key)
    if cached is not None:
        log.info("omnichannel.mediaurl.cache_hit", extra={"org_id": org_id})
        return str(cached)

    url = generate_signed_url(s3_key, expires_in=_SIGN_EXPIRY_SECONDS)
    await redis.set(cache_key, url, ex=_CACHE_TTL_SECONDS)
    log.info("omnichannel.mediaurl.cache_miss", extra={"org_id": org_id})
    return url
