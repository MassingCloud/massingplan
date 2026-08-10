"""Second-factor columns on the user.

The secret is `Text` and nullable: nullable because most accounts will not have
one, and `Text` because it holds a Fernet token rather than the base32 secret --
ciphertext is longer than the plaintext and a `String(64)` would truncate it on
MySQL and raise on Postgres, in both cases at enrolment time.

`mfa_recovery_hashes` defaults to an empty JSON array rather than NULL so that
`len(user.mfa_recovery_hashes)` works on rows that predate this migration.

Revision ID: 0003_mfa
Revises: 0002_identity
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_mfa"
down_revision = "0002_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mfa_secret_encrypted", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "mfa_recovery_hashes",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column("mfa_enabled_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("mfa_last_code", sa.String(length=12), nullable=True))
        batch_op.add_column(
            sa.Column("mfa_last_code_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    # Dropping these discards every enrolled secret, so a downgrade-then-upgrade
    # leaves those users locked out of their second factor rather than merely
    # back on the old schema. Say so here; `docs/deployment.md` says it louder.
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("mfa_last_code_at")
        batch_op.drop_column("mfa_last_code")
        batch_op.drop_column("mfa_enabled_at")
        batch_op.drop_column("mfa_recovery_hashes")
        batch_op.drop_column("mfa_secret_encrypted")
