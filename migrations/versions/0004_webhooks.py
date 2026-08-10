"""Outbound webhook endpoints, and the outbox that feeds them.

Two shapes worth explaining.

`webhook_deliveries` is one row per (event x endpoint), not per event. Two
subscribers to the same event fail independently, and a shared row cannot
express "delivered to one, still retrying the other".

`ix_delivery_due` on (status, next_attempt_at) is the drain query's index.
Without it, picking up work is a full scan of a table that only ever grows, and
the drain gets slower every day while looking like it is doing the same work.

Revision ID: 0004_webhooks
Revises: 0003_mfa
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from massingplan.models.base import UtcDateTime

revision = "0004_webhooks"
down_revision = "0003_mfa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhooks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("created_by_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("secret_stored", sa.Text(), nullable=False),
        sa.Column("secret_encrypted", sa.Boolean(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("disabled_at", UtcDateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.String(length=300), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_success_at", UtcDateTime(timezone=True), nullable=True),
        sa.Column("created_at", UtcDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", UtcDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("webhooks", schema=None) as batch_op:
        batch_op.create_index(
            "ix_webhook_org_active", ["organization_id", "is_active"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_webhooks_organization_id"), ["organization_id"], unique=False
        )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("webhook_id", sa.String(length=32), nullable=False),
        sa.Column("event", sa.String(length=60), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "delivered",
                "failed",
                name="delivery_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", UtcDateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", UtcDateTime(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_excerpt", sa.String(length=600), nullable=False),
        sa.Column("error", sa.String(length=300), nullable=False),
        sa.Column("created_at", UtcDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", UtcDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["webhook_id"], ["webhooks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.create_index("ix_delivery_due", ["status", "next_attempt_at"], unique=False)
        batch_op.create_index(
            "ix_delivery_org_created", ["organization_id", "created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_webhook_deliveries_organization_id"), ["organization_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_webhook_deliveries_webhook_id"), ["webhook_id"], unique=False
        )


def downgrade() -> None:
    # Drops the subscriptions and every queued event with them. A downgrade
    # loses undelivered events outright; there is nowhere else they are kept.
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_webhook_deliveries_webhook_id"))
        batch_op.drop_index(batch_op.f("ix_webhook_deliveries_organization_id"))
        batch_op.drop_index("ix_delivery_org_created")
        batch_op.drop_index("ix_delivery_due")
    op.drop_table("webhook_deliveries")

    with op.batch_alter_table("webhooks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_webhooks_organization_id"))
        batch_op.drop_index("ix_webhook_org_active")
    op.drop_table("webhooks")
