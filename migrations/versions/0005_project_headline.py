"""The project-list headline, denormalised onto `projects`.

Rendering the list from the activity rows meant loading every activity of every
project -- twenty projects of a thousand activities is twenty thousand ORM
objects to draw twenty table rows, and it degrades with exactly the thing a
growing customer has more of. These six columns make the list one query over one
table with no children loaded.

**The backfill is the point of this migration, not the columns.** Adding them
empty would leave every existing project reading "not scheduled yet" until
somebody opened it -- a silent regression that looks like data loss to the user
and like nothing at all in CI, where the tables start empty.

`server_default` on the two counts because they are NOT NULL and the table has
rows: without it the ALTER fails on any database that is actually in use, and
succeeds on every empty test one.

Revision ID: 0005_project_headline
Revises: 0004_webhooks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_project_headline"
down_revision = "0004_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("computed_start", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("computed_finish", sa.Date(), nullable=True))
        batch_op.add_column(
            sa.Column("activity_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("critical_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("baseline_name", sa.String(length=120), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("baseline_finish", sa.Date(), nullable=True))

    # `WHERE a.is_critical` rather than `= 1`: Postgres refuses to compare a
    # boolean to an integer, and SQLite treats any non-zero value as true, so
    # the bare column is the form that means the same thing on both.
    #
    # Backfill from what is already stored. Correlated subqueries rather than a
    # Python loop: this runs once per deployment against a whole table, and
    # loading every project into the migration process to compute six values is
    # the same mistake the columns exist to fix.
    op.execute(
        """
        UPDATE projects SET
            computed_start = (
                SELECT MIN(a.computed_start) FROM activities a
                WHERE a.project_id = projects.id AND a.computed_start IS NOT NULL
            ),
            computed_finish = (
                SELECT MAX(a.computed_finish) FROM activities a
                WHERE a.project_id = projects.id AND a.computed_finish IS NOT NULL
            ),
            activity_count = (
                SELECT COUNT(*) FROM activities a WHERE a.project_id = projects.id
            ),
            critical_count = (
                SELECT COUNT(*) FROM activities a
                WHERE a.project_id = projects.id AND a.is_critical
            )
        """
    )
    op.execute(
        """
        UPDATE projects SET
            baseline_name = COALESCE((
                SELECT b.name FROM baselines b
                WHERE b.project_id = projects.id AND b.is_current
            ), ''),
            baseline_finish = (
                SELECT b.project_finish FROM baselines b
                WHERE b.project_id = projects.id AND b.is_current
            )
        """
    )


def downgrade() -> None:
    # Safe, unusually: every value here is derivable from the activity and
    # baseline rows, which this does not touch.
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("baseline_finish")
        batch_op.drop_column("baseline_name")
        batch_op.drop_column("critical_count")
        batch_op.drop_column("activity_count")
        batch_op.drop_column("computed_finish")
        batch_op.drop_column("computed_start")
