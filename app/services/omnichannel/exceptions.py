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
    """The routing strategy could not assign a conversation."""

    status_code = 500


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
