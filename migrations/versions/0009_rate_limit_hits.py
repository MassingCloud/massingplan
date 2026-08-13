"""The shared rate-limit counter.

One table so N workers count against one limit instead of N. `SECURITY.md`
carried this as its loudest limitation: with `WEB_CONCURRENCY=4` the effective
limit was four times the configured one, and a limiter that silently multiplies
is worse than none because it is believed.

The unique constraint on `(key, window_start)` is not decoration — it is the
upsert target. `INSERT ... ON CONFLICT DO UPDATE SET hits = hits + 1` is what
makes the increment atomic, and without the constraint two workers racing the
same key insert two rows and each counts half the traffic, which is the
per-process bug wearing a different hat.

`window_start` is whole seconds of **wall-clock** time. A monotonic clock has a
per-process origin, so workers would disagree about which window an instant
belongs to.

Nothing here is durable data: rows live for one window and
`DatabaseStore.hit` sweeps closed ones. The index on `window_start` is for that
delete.

Revision ID: 0009_rate_limit_hits
Revises: 0008_lastplanner
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_rate_limit_hits"
down_revision = "0008_lastplanner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_hits",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=320), nullable=False),
        sa.Column("window_start", sa.BigInteger(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", "window_start", name="uq_rate_limit_window"),
    )
    with op.batch_alter_table("rate_limit_hits", schema=None) as batch_op:
        batch_op.create_index("ix_rate_limit_window_start", ["window_start"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("rate_limit_hits", schema=None) as batch_op:
        batch_op.drop_index("ix_rate_limit_window_start")
    op.drop_table("rate_limit_hits")
