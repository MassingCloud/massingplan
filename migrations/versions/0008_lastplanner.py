"""Weekly work plans, commitments, and the constraint log.

`weekly_plans` is a table rather than a date on each commitment because **the
week is the thing that gets frozen**. PPC's denominator is every commitment
made for that week, and if a week were only an attribute of its commitments,
deleting a commitment would silently shrink the denominator -- the single
commonest way the metric is gamed. Unique on `(project_id, week_starting)`,
because two plans for one week is two denominators for one number.

`lp_commitments.completed` is nullable on purpose. "Not yet assessed" is a
third state, and storing it as false reports an unfinished week as a failed
one.

`lp_constraints.removed_on` is a date rather than a boolean, because the engine
reads a removal date in the future as *still live today* -- a constraint
cleared next Friday must not make this Monday's plan look ready.

`activity_code` is a string, not a foreign key: a commitment can be made
against work that has not been added to the CPM schedule yet, which is normal
in a lookahead and is not a reason to refuse to record the promise.

Three new tables. No existing row is touched.

Revision ID: 0008_lastplanner
Revises: 0007_sso_subject
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_lastplanner"
down_revision = "0007_sso_subject"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weekly_plans",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("week_starting", sa.Date(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "week_starting", name="uq_weekly_plan_week"),
    )
    with op.batch_alter_table("weekly_plans", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_weekly_plans_organization_id"), ["organization_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_weekly_plans_project_id"), ["project_id"], unique=False)
        batch_op.create_index(
            "ix_weekly_plan_project_week", ["project_id", "week_starting"], unique=False
        )

    op.create_table(
        "lp_commitments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("plan_id", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("activity_code", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("description", sa.String(length=400), nullable=False),
        sa.Column("crew", sa.String(length=120), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=True),
        sa.Column("reason", sa.String(length=40), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["weekly_plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("lp_commitments", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_lp_commitments_organization_id"), ["organization_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_lp_commitments_plan_id"), ["plan_id"], unique=False)
        batch_op.create_index(
            "ix_commitment_plan_sequence", ["plan_id", "sequence"], unique=False
        )

    op.create_table(
        "lp_constraints",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("commitment_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("description", sa.String(length=400), nullable=False),
        sa.Column("owner", sa.String(length=120), nullable=False),
        sa.Column("promised_by", sa.Date(), nullable=False),
        sa.Column("removed_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["commitment_id"], ["lp_commitments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("lp_constraints", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_lp_constraints_organization_id"), ["organization_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_lp_constraints_commitment_id"), ["commitment_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("lp_constraints", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_lp_constraints_commitment_id"))
        batch_op.drop_index(batch_op.f("ix_lp_constraints_organization_id"))
    op.drop_table("lp_constraints")

    with op.batch_alter_table("lp_commitments", schema=None) as batch_op:
        batch_op.drop_index("ix_commitment_plan_sequence")
        batch_op.drop_index(batch_op.f("ix_lp_commitments_plan_id"))
        batch_op.drop_index(batch_op.f("ix_lp_commitments_organization_id"))
    op.drop_table("lp_commitments")

    with op.batch_alter_table("weekly_plans", schema=None) as batch_op:
        batch_op.drop_index("ix_weekly_plan_project_week")
        batch_op.drop_index(batch_op.f("ix_weekly_plans_project_id"))
        batch_op.drop_index(batch_op.f("ix_weekly_plans_organization_id"))
    op.drop_table("weekly_plans")
