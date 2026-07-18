"""Omni-Channel error hierarchy, wired to Core's (app/services/omnichannel/CLAUDE.md §8).

Each subclass carries a ``status_code`` exactly as Core's errors do; routers
map it straight to an HTTP response with zero new plumbing. ``RateLimitError``,
``SuppressionListError``, etc. are raised *by Core* and pass through untouched.
"""

from __future__ import annotations

from app.core.exceptions import CoreError


class OmniChannelError(CoreError):
    """Base for all Omni-Channel-specific errors."""

    status_code = 500


class ChannelAdapterError(OmniChannelError):
    """A channel adapter failed to send, normalize, or interpret a payload."""

    status_code = 502


class WebhookSignatureError(OmniChannelError):
    """An inbound webhook's signature did not verify."""

    status_code = 401


class ConnectionNotFoundError(OmniChannelError):
    """No channel connection exists for the given connection_id (§5.6).

    Not in the original §8 list -- added when building the generic webhook
    route (Step 5): resolving ``connection_id`` to an org needs a distinct
    404 from ``WebhookSignatureError``'s 401, since an unknown connection
    was never signed by anyone in the first place.
    """

    status_code = 404


class RoutingError(OmniChannelError):
    """The requested routing configuration is invalid (bad/unsupported strategy,
    or a required field is missing) -- always a client input problem, not an
    engine failure.

    DOCUMENTED CORRECTION (API review, 2026-07-18): originally specced as a
    generic 500 (CLAUDE.md §8), but every actual raise site
    (``routing.py::set_routing_config``) is a request-validation failure, and
    the published API reference already documented this as 400. Fixed here
    to match reality rather than "fixing" the docs back to a 500 that was
    never earned by an actual internal-failure code path.
    """

    status_code = 400


class CommissionError(OmniChannelError):
    """A commission attribution could not be recorded or reconciled."""

    status_code = 409


class ConversationNotFoundError(OmniChannelError):
    """Requested conversation does not exist for this org."""

    status_code = 404


class ForbiddenError(OmniChannelError):
    """Caller's role can't perform this action (§4).

    Originally defined in ``handlers.py`` for ``send_reply``; moved here in
    Step 6 once ``routing.py`` needed the same class for claim/reassign/
    routing-config authz. ``handlers.py`` re-exports it via import so
    existing ``from app.services.omnichannel.handlers import ForbiddenError``
    call sites keep working.
    """

    status_code = 403


class ConversationAlreadyAssignedError(OmniChannelError):
    """A claim was attempted on a conversation already assigned to someone else.

    Not in the original §8 list -- added when building assignment (Step 6):
    distinguishes "already taken, use reassign instead" from a generic
    500-level ``RoutingError``.
    """

    status_code = 409


class InvalidQueryError(OmniChannelError):
    """A list/search query param is malformed: an unknown ``sort`` value or a
    cursor that doesn't decode to a valid (timestamp, id) pair.

    Added with keyset pagination (API review, 2026-07-18): a tampered or
    stale cursor must fail as a client error, not a 500 from an unhandled
    decode exception.
    """

    status_code = 400


class ConnectionValidationError(OmniChannelError):
    """A channel-connection create/update request is invalid: an unregistered
    ``channel_type`` (§5.2 -- only adapters actually in the registry may be
    connected), or a ``credentials_secret_key`` that doesn't resolve to an
    existing secret.

    Added with the connections CRUD API (API review, 2026-07-18).
    """

    status_code = 400
