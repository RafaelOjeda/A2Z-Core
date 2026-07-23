"""Invoicing error hierarchy, wired to Core's (app/services/invoicing/CLAUDE.md §8).

Each subclass carries a ``status_code`` exactly as Core's errors do; routers
map it straight to an HTTP response with zero new plumbing (the single
``CoreError`` handler in ``app.main``). ``SuppressionListError``,
``RateLimitError``, ``FileTooLargeError``, etc. are raised *by Core* and pass
through untouched.
"""

from __future__ import annotations

from app.core.exceptions import CoreError


class InvoicingError(CoreError):
    """Base for all Invoicing-specific errors."""

    status_code = 500


class InvoiceNotFoundError(InvoicingError):
    """No invoice exists for the given id in this org (or it's soft-deleted).

    Also raised when an invoice belongs to a different org -- cross-org
    existence is itself information we don't hand out (mirrors
    Omni-Channel's ``ConversationNotFoundError`` convention).
    """

    status_code = 404


class InvoiceForbiddenError(InvoicingError):
    """Caller's role doesn't permit the requested action (§4: OWNER/ADMIN only)."""

    status_code = 403


class InvalidStateTransitionError(InvoicingError):
    """The requested lifecycle move is illegal from the invoice's current status.

    E.g. sending an already-sent invoice, recording a payment on a draft,
    voiding an already-void invoice, or editing a void invoice (§3.1).
    """

    status_code = 409


class InvoiceValidationError(InvoicingError):
    """Bad input: empty line items, non-positive quantity/price, malformed
    recipient, or totals that don't reconcile."""

    status_code = 400
