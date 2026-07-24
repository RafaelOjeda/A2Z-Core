"""Create baseline invoicing schema.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create invoices, invoice_line_items, and invoice_payments tables."""
    op.create_table(
        "invoices",
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("invoice_id", sa.String(36), nullable=False),
        sa.Column("invoice_number", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("customer_name", sa.String(255), nullable=False),
        sa.Column("customer_email", sa.String(255), nullable=False),
        sa.Column("customer_company", sa.String(255), nullable=True),
        sa.Column("invoice_date", sa.DATE(), nullable=False),
        sa.Column("due_date", sa.DATE(), nullable=False),
        sa.Column("payment_terms", sa.String(255), nullable=True),
        sa.Column("subtotal_cents", sa.BigInteger(), nullable=False),
        sa.Column("tax_cents", sa.BigInteger(), nullable=False),
        sa.Column("discount_cents", sa.BigInteger(), nullable=False),
        sa.Column("total_cents", sa.BigInteger(), nullable=False),
        sa.Column("paid_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("pdf_key", sa.String(500), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("void_reason", sa.String(500), nullable=True),
        sa.Column("voided_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("org_id", "invoice_id"),
        sa.UniqueConstraint("org_id", "invoice_number", name="uq_org_invoice_number"),
        schema="invoicing",
    )
    op.create_index("ix_invoices_org_status", "invoices", ["org_id", "status"], schema="invoicing")
    op.create_index(
        "ix_invoices_org_created", "invoices", ["org_id", "created_at"], schema="invoicing"
    )

    op.create_table(
        "invoice_line_items",
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("line_item_id", sa.String(36), nullable=False),
        sa.Column("invoice_id", sa.String(36), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("quantity", sa.DECIMAL(precision=15, scale=4), nullable=False),
        sa.Column("unit_price_cents", sa.BigInteger(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoicing.invoices.invoice_id"],
        ),
        sa.PrimaryKeyConstraint("org_id", "line_item_id"),
        schema="invoicing",
    )
    op.create_index(
        "ix_line_items_invoice", "invoice_line_items", ["invoice_id"], schema="invoicing"
    )

    op.create_table(
        "invoice_payments",
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("payment_id", sa.String(36), nullable=False),
        sa.Column("invoice_id", sa.String(36), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("payment_date", sa.DATE(), nullable=False),
        sa.Column("method", sa.String(50), nullable=False),
        sa.Column("reference", sa.String(255), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoicing.invoices.invoice_id"],
        ),
        sa.PrimaryKeyConstraint("org_id", "payment_id"),
        schema="invoicing",
    )
    op.create_index("ix_payments_invoice", "invoice_payments", ["invoice_id"], schema="invoicing")


def downgrade() -> None:
    """Drop all invoicing tables."""
    op.drop_index("ix_payments_invoice", schema="invoicing")
    op.drop_table("invoice_payments", schema="invoicing")

    op.drop_index("ix_line_items_invoice", schema="invoicing")
    op.drop_table("invoice_line_items", schema="invoicing")

    op.drop_index("ix_invoices_org_created", schema="invoicing")
    op.drop_index("ix_invoices_org_status", schema="invoicing")
    op.drop_table("invoices", schema="invoicing")
