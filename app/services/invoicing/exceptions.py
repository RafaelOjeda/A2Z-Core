"""Invoicing error hierarchy, wired to Core's (app/services/invoicing/CLAUDE.md §8).

Each subclass carries a ``status_code`` exactly as Core's errors do; routers
map it straight to an HTTP response with zero new plumbing.
"""

from __future__ import annotations

from app.core.exceptions import CoreError


class InvoicingError(CoreError):
    """Base for all Invoicing-specific errors."""

    status_code = 500


class InvoiceNotFoundError(InvoicingError):
    """No invoice exists for the given invoice_id in this org."""

    status_code = 404


class InvalidStateTransitionError(InvoicingError):
    """Attempted an illegal invoice state transition (e.g., void → sent, payment on draft)."""

    status_code = 409


class InvoiceStatusError(InvoicingError):
    """A mutation is incompatible with the invoice's current status."""

    status_code = 409


class InvalidLineItemError(InvoicingError):
    """A line item is malformed (negative quantity, invalid price, etc.)."""

    status_code = 400


class PDFGenerationError(InvoicingError):
    """PDF rendering failed."""

    status_code = 500
