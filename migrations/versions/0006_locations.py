"""The location breakdown, and the trades that flow through it.

`locations` and `linear_activities` are both ordered by an explicit `sequence`,
indexed with the project id: location order is the direction of flow and trade
order is the handover sequence, so both are read in order on every page that
shows them.

`linear_quantities` is a table rather than a JSON column on the trade, for the
reason the baseline tables are rows: a blob can be stored and never queried, and
a quantity needs a real foreign key to the location it belongs to so renaming a
level cannot orphan it.

Nothing here is required. A CPM project has no location model, and every column
is on a new table -- so this migration adds capability without touching a single
existing row.

Revision ID: 0006_locations
Revises: 0005_project_headline
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from massingplan.models.base import UtcDateTime

revision = "0006_locations"
down_revision = "0005_project_headline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "linear_activities",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("rate", sa.Float(), nullable=True),
        sa.Column("buffer_days", sa.Integer(), nullable=False),
        sa.Column("crews", sa.Integer(), nullable=False),
        sa.Column("calendar_key", sa.String(length=32), nullable=False),
        sa.Column("created_at", UtcDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", UtcDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "key", name="uq_linear_project_key"),
    )
    with op.batch_alter_table("linear_activities", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_linear_activities_organization_id"), ["organization_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_linear_activities_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(
            "ix_linear_project_sequence", ["project_id", "sequence"], unique=False
        )

    op.create_table(
        "locations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", UtcDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", UtcDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "key", name="uq_location_project_key"),
    )
    with op.batch_alter_table("locations", schema=None) as batch_op:
        batch_op.create_index(
            "ix_location_project_sequence", ["project_id", "sequence"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_locations_organization_id"), ["organization_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_locations_project_id"), ["project_id"], unique=False)

    op.create_table(
        "linear_quantities",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("activity_id", sa.String(length=32), nullable=False),
        sa.Column("location_id", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["activity_id"], ["linear_activities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("activity_id", "location_id", name="uq_linear_quantity"),
    )
    with op.batch_alter_table("linear_quantities", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_linear_quantities_activity_id"), ["activity_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_linear_quantities_location_id"), ["location_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("linear_quantities", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_linear_quantities_location_id"))
        batch_op.drop_index(batch_op.f("ix_linear_quantities_activity_id"))

    op.drop_table("linear_quantities")
    with op.batch_alter_table("locations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_locations_project_id"))
        batch_op.drop_index(batch_op.f("ix_locations_organization_id"))
        batch_op.drop_index("ix_location_project_sequence")

    op.drop_table("locations")
    with op.batch_alter_table("linear_activities", schema=None) as batch_op:
        batch_op.drop_index("ix_linear_project_sequence")
        batch_op.drop_index(batch_op.f("ix_linear_activities_project_id"))
        batch_op.drop_index(batch_op.f("ix_linear_activities_organization_id"))

    op.drop_table("linear_activities")
