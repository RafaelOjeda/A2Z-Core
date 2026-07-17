"""Integration tests for the cached signed-URL helper (§10, Build Order Step 8).

Real moto S3 signing + real fakeredis caching -- no mocking of either side.
"""

from __future__ import annotations

import pytest

from app.core import clients
from app.core.exceptions import StorageError
from app.services.omnichannel import media

pytestmark = pytest.mark.integration


async def test_returns_signed_url_for_own_key(aws: None) -> None:
    url = await media.signed_url_for_attachment("org-a", "org-a/omnichannel/photo.jpg")
    assert url.startswith("http")
    assert "org-a/omnichannel/photo.jpg" in url
    assert "Signature" in url or "X-Amz-Signature" in url


async def test_second_call_returns_cached_url(aws: None) -> None:
    """A repeat request returns the *same* URL -- the point of the cache (§10):
    a fresh signature each time would bust the browser's HTTP cache."""
    first = await media.signed_url_for_attachment("org-a", "org-a/omnichannel/photo.jpg")
    second = await media.signed_url_for_attachment("org-a", "org-a/omnichannel/photo.jpg")
    assert first == second


async def test_cache_ttl_is_shorter_than_signed_expiry(aws: None) -> None:
    """Invariant 1: a cache hit must never hand back a URL that's about to expire."""
    assert media._CACHE_TTL_SECONDS < media._SIGN_EXPIRY_SECONDS

    await media.signed_url_for_attachment("org-a", "org-a/omnichannel/photo.jpg")
    ttl = await clients.redis_client().ttl("mediaurl:org-a/omnichannel/photo.jpg")
    assert 0 < ttl <= media._CACHE_TTL_SECONDS


async def test_cross_org_key_is_rejected(aws: None) -> None:
    """Invariant 2: core.storage.generate_signed_url has no org check of its
    own, so this module must refuse another org's key itself."""
    with pytest.raises(StorageError):
        await media.signed_url_for_attachment("org-a", "org-b/omnichannel/secret.jpg")


async def test_cross_org_rejection_happens_before_caching(aws: None) -> None:
    """A rejected key must not leave anything usable behind in the cache."""
    with pytest.raises(StorageError):
        await media.signed_url_for_attachment("org-a", "org-b/omnichannel/secret.jpg")

    assert await clients.redis_client().get("mediaurl:org-b/omnichannel/secret.jpg") is None


async def test_orgs_get_independent_cache_entries(aws: None) -> None:
    a = await media.signed_url_for_attachment("org-a", "org-a/omnichannel/x.jpg")
    b = await media.signed_url_for_attachment("org-b", "org-b/omnichannel/x.jpg")
    assert a != b
    assert "org-a/omnichannel/x.jpg" in a
    assert "org-b/omnichannel/x.jpg" in b
