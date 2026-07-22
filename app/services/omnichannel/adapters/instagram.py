"""Instagram DM channel adapter -- Meta Messenger Platform (Graph API).

CLAUDE.md §5.2, §15. Instagram Direct Messages run on the *same* Messenger
Platform as Facebook Messenger: the same ``X-Hub-Signature-256`` signing, the
same ``hub.challenge`` subscription handshake, the same
``entry[].messaging[]`` inbound shape, and the same
``recipient``/``message`` Send API body. So this adapter is almost entirely
``MessengerPlatformAdapter`` -- it differs only in which credential holds the
sending account id.

For Instagram the Send API path is ``/{ig_id}/messages``, where ``ig_id`` is
the Instagram professional-account id linked to the connected Page; the
access token is still that Page's ``page_access_token``. Credentials
(``app_secret``, ``verify_token``, ``page_access_token``, ``ig_id``) are
per-org via ``core.secrets`` (§6.2).

The v1 scope inherited from ``MessengerPlatformAdapter`` applies unchanged:
text-only outbound inside the messaging window, inbound media recorded with a
placeholder body, no inline sender name.
"""

from __future__ import annotations

from app.services.omnichannel.adapters.messenger import MessengerPlatformAdapter


class InstagramAdapter(MessengerPlatformAdapter):
    """Instagram DM leaf -- account id is the IG professional-account id."""

    _account_id_key = "ig_id"
