"""Where an SSO user's identity actually lives.

One nullable column on `users`, holding `issuer#sub` -- the identity provider's
own name for the person, namespaced by the issuer that asserted it.

**Not the email address.** An email at an identity provider is mutable and
re-assignable: somebody leaves, the address is reallocated to a new starter six
months later, and matching on it hands the new person the old person's
projects. `sub` is the only claim OIDC guarantees is stable and unique within an
issuer, and the issuer prefix is what stops two providers that both number
their users from 1 colliding.

Unique, so two accounts cannot claim one external identity. Nullable, because
every existing user has a password and no external identity at all -- this
migration touches no existing row.

Revision ID: 0007_sso_subject
Revises: 0006_locations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_sso_subject"
down_revision = "0006_locations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sso_subject", sa.String(length=512), nullable=True))
        # Unique rather than merely indexed. Without it, two rows can claim the
        # same external identity and which one signs in becomes a question of
        # row order -- a bug that appears only after the second account exists.
        batch_op.create_index("ix_user_sso_subject", ["sso_subject"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_user_sso_subject")
        batch_op.drop_column("sso_subject")
