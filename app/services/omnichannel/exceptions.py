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


class RoutingError(OmniChannelError):
    """The routing strategy could not assign a conversation."""

    status_code = 500


class CommissionError(OmniChannelError):
    """A commission attribution could not be recorded or reconciled."""

    status_code = 409


class ConversationNotFoundError(OmniChannelError):
    """Requested conversation does not exist for this org."""

    status_code = 404
