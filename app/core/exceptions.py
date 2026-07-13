"""CoreError hierarchy (Design §6).

Every Core function raises one of these typed errors — never a bare ``Exception``
and never an error dict. Each carries a ``status_code`` so the thin router layer
can map it straight to an HTTP response.
"""

from __future__ import annotations


class CoreError(Exception):
    """Base exception for all Core errors."""

    status_code: int = 500


# --- Auth (401) ---
class AuthError(CoreError):
    status_code = 401


class InvalidTokenError(AuthError):
    """JWT invalid, expired, or signature does not match."""


class MissingTokenError(AuthError):
    """No Authorization header / token provided."""


# --- Membership (400 / 404 / 409) ---
class MembershipError(CoreError):
    status_code = 400


class NotFoundError(MembershipError):
    """Requested resource does not exist."""

    status_code = 404


class AlreadyExistsError(MembershipError):
    """Resource already exists (e.g. user already in org)."""

    status_code = 409


# --- Email (400 / 429) ---
class EmailError(CoreError):
    status_code = 400


class SuppressionListError(EmailError):
    """Recipient is on the org's bounce/complaint suppression list."""


class InvalidAddressError(EmailError):
    """Recipient email address is invalid."""


class RateLimitError(CoreError):
    """A rate limit was exceeded.

    Carries ``retry_after`` (seconds) so callers/HTTP layer can set Retry-After.
    """

    status_code = 429

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# --- Storage (400 / 404) ---
class StorageError(CoreError):
    status_code = 400


class FileTooLargeError(StorageError):
    """Uploaded file exceeds the maximum allowed size."""


class StorageNotFoundError(StorageError):
    """Requested file does not exist."""

    status_code = 404


# --- Settings (400) ---
class SettingsError(CoreError):
    status_code = 400


# --- Audit (500) ---
class AuditError(CoreError):
    status_code = 500


# --- Events (500) ---
class EventError(CoreError):
    status_code = 500


# --- Secrets (400 / 404) ---
class SecretsError(CoreError):
    status_code = 400


class SecretNotFoundError(SecretsError):
    """No secret exists at the given org/service/key."""

    status_code = 404


# --- Realtime (502) ---
class RealtimeError(CoreError):
    """Failed to publish a real-time update (config/connectivity failure)."""

    status_code = 502
