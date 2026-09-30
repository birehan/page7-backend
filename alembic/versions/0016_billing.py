"""Billing tables (Phase 13)

Creates plans, subscriptions, invoices, and invoice_counters
(architecture/02 §15). ai_usage_counters already shipped in 0008.

Revision ID: 0016_billing
Revises: 0015_zernio_credential_capacity
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016_billing"
down_revision: str | None = "0015_zernio_credential_capacity"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due", "canceled")
_INVOICE_STATUSES = ("draft", "pending", "paid", "failed", "void", "refunded")


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("price_monthly_sar", sa.Numeric(10, 2), nullable=False),
        sa.Column("ai_generations_monthly", sa.Integer(), nullable=False),
        sa.Column("brands_limit", sa.Integer(), nullable=False),
        sa.Column("seats_limit", sa.Integer(), nullable=False),
        sa.Column("channels_limit", sa.Integer(), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_plans")),
    )

    op.create_table(
        "subscriptions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("plan_code", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("price_monthly_sar", sa.Numeric(10, 2), nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("psp", sa.Text(), nullable=True),
        sa.Column("psp_customer_id", sa.Text(), nullable=True),
        sa.Column("psp_subscription_id", sa.Text(), nullable=True),
        sa.Column(
            "payment_method",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _SUBSCRIPTION_STATUSES) + ")",
            name=op.f("ck_subscriptions_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_subscriptions_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_code"],
            ["plans.code"],
            name=op.f("fk_subscriptions_plan_code_plans"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
    )
    op.create_index(
        "ux_subscription_current",
        "subscriptions",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('trialing', 'active', 'past_due')"
        ),
    )

    op.create_table(
        "invoices",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=True),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "currency",
            sa.CHAR(3),
            server_default="SAR",
            nullable=False,
        ),
        sa.Column("amount_subtotal", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "vat_rate",
            sa.Numeric(5, 4),
            server_default=sa.text("0.1500"),
            nullable=False,
        ),
        sa.Column("vat_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("total", sa.Numeric(12, 2), nullable=False),
        sa.Column("seller_vat_number", sa.Text(), nullable=False),
        sa.Column("buyer_vat_number", sa.Text(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("psp_payment_id", sa.Text(), nullable=True),
        sa.Column(
            "psp_raw",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("pdf_r2_key", sa.Text(), nullable=True),
        sa.Column("zatca_uuid", sa.Text(), nullable=True),
        sa.Column("zatca_hash", sa.Text(), nullable=True),
        sa.Column("qr_payload", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _INVOICE_STATUSES) + ")",
            name=op.f("ck_invoices_status_valid"),
        ),
        sa.CheckConstraint(
            "currency = 'SAR'",
            name=op.f("ck_invoices_currency_sar"),
        ),
        sa.CheckConstraint(
            "total = amount_subtotal + vat_amount",
            name=op.f("ck_invoices_total_equals_parts"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_invoices_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_invoices_subscription_id_subscriptions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
        sa.UniqueConstraint("number", name=op.f("uq_invoices_number")),
    )
    op.create_index(
        "ix_invoices_org_issued",
        "invoices",
        ["organization_id", sa.literal_column("issued_at DESC")],
        unique=False,
    )

    op.create_table(
        "invoice_counters",
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("last_number", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("issuer", name=op.f("pk_invoice_counters")),
    )

    # Usage-alert notifications (Phase 13 billing.usage_alerts).
    op.drop_constraint(
        op.f("ck_notifications_type_valid"), "notifications", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_notifications_type_valid"),
        "notifications",
        "type IN ("
        "'approval_requested', 'post_approved', 'post_rejected', 'post_published', "
        "'post_failed', 'changes_requested', 'channel_expiring', 'channel_disconnected', "
        "'weekly_insight', 'plan_generated', 'mention', 'ai_credits_warning')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_notifications_type_valid"), "notifications", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_notifications_type_valid"),
        "notifications",
        "type IN ("
        "'approval_requested', 'post_approved', 'post_rejected', 'post_published', "
        "'post_failed', 'changes_requested', 'channel_expiring', 'channel_disconnected', "
        "'weekly_insight', 'plan_generated', 'mention')",
    )
    op.drop_table("invoice_counters")
    op.drop_index("ix_invoices_org_issued", table_name="invoices")
    op.drop_table("invoices")
    op.drop_index("ux_subscription_current", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("plans")
