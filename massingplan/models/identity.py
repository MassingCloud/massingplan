"""Users, organisation membership, API keys and the audit log.

Separate from `schedule.py` because the two answer different questions and
change for different reasons: one is what the schedule is, the other is who may
touch it.

Three decisions worth naming:

**Email is unique per organisation, not globally.** A global unique means one
tenant's sign-up fails because a different tenant already used that address, and
the error message confirms an account exists somewhere they cannot see. It also
makes the same person's two employers mutually exclusive.

**A user's role lives on the membership, not the user.** Somebody can be an
owner of one organisation and a viewer of another, which a role column on
`users` cannot express without lying.

**Audit rows are append-only and reference the actor by id, not by name.**
Names change; the question "who approved this baseline" must still be answerable
in two years.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, DateTime, TimestampMixin, org_column, pk_column
from .schedule import enum_column


class Role(str, Enum):
    """Written out in full per role, with no inheritance.

    ``ADMIN`` is not "OWNER minus one thing" -- it is its own list. Inheritance
    means widening one role silently widens every role above it, and the person
    making the change sees only the line they edited.
    """

    OWNER = "owner"
    ADMIN = "admin"
    PLANNER = "planner"
    VIEWER = "viewer"


class Permission(str, Enum):
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    PROJECT_DELETE = "project:delete"
    BASELINE_SET = "baseline:set"
    IMPORT_RUN = "import:run"
    MEMBER_MANAGE = "member:manage"
    KEY_MANAGE = "key:manage"
    #: Separate from KEY_MANAGE, because they give away different things. A key
    #: lets its holder read this organisation's data; a webhook makes the server
    #: issue outbound requests to an address of the subscriber's choosing, and
    #: mails them every event thereafter. Owner and admin only.
    WEBHOOK_MANAGE = "webhook:manage"


#: Each role's permissions, spelled out. Deliberately repetitive: a reader can
#: answer "what can a planner do" by reading one line, and widening PLANNER
#: cannot reach VIEWER by accident.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.ADMIN: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.PROJECT_WRITE,
            Permission.PROJECT_DELETE,
            Permission.BASELINE_SET,
            Permission.IMPORT_RUN,
            Permission.MEMBER_MANAGE,
            Permission.WEBHOOK_MANAGE,
        }
    ),
    Role.PLANNER: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.PROJECT_WRITE,
            Permission.BASELINE_SET,
            Permission.IMPORT_RUN,
        }
    ),
    Role.VIEWER: frozenset({Permission.PROJECT_READ}),
}


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (Index("ix_user_email", "email"),)

    id: Mapped[str] = pk_column()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: argon2id. The algorithm and its parameters are encoded in the hash, so a
    #: parameter change re-hashes on next sign-in rather than needing a column.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Consecutive failures, reset on success. Lock-out is by count rather than
    #: by IP: an attacker rotates addresses, and the account is what needs
    #: protecting.
    failed_sign_ins: Mapped[int] = mapped_column(default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- second factor -----------------------------------------------------
    #: Encrypted, not hashed: verification needs the secret back. Encrypted
    #: rather than plain because a leaked backup would otherwise hand over a
    #: second factor the attacker can compute -- and the key lives in the
    #: environment, so it does not travel with the dump.
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Hashes. Storing recovery codes readably makes the list a second copy of
    #: the second factor.
    mfa_recovery_hashes: Mapped[list] = mapped_column(JSON, default=list)
    mfa_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The last code accepted, and when. Without these a captured code is
    #: replayable for the ninety seconds its window stays open, which is longer
    #: than an attacker needs.
    mfa_last_code: Mapped[str | None] = mapped_column(String(12), nullable=True)
    mfa_last_code_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    def membership_in(self, organization_id: str) -> Membership | None:
        return next((m for m in self.memberships if m.organization_id == organization_id), None)


class Membership(Base, TimestampMixin):
    """A user's role in one organisation."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_membership"),
        # Email unique *within* an organisation. See the module docstring.
        UniqueConstraint("organization_id", "email", name="uq_membership_org_email"),
    )

    id: Mapped[str] = pk_column()
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    organization_id: Mapped[str] = org_column()
    #: Denormalised from the user so the uniqueness constraint above can exist
    #: at all -- a cross-table unique is not expressible. Kept in step by the
    #: accounts service, which is the only writer.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[Role] = mapped_column(
        enum_column(Role, "role"), default=Role.VIEWER, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="memberships")

    @property
    def permissions(self) -> frozenset[Permission]:
        return ROLE_PERMISSIONS[self.role]

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions


class ApiKey(Base, TimestampMixin):
    """A machine credential, scoped to one organisation.

    Only the hash is stored. A database of usable credentials is a breach
    waiting for a backup to leak, and the plaintext is shown exactly once.
    """

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_key_hash", "key_hash", unique=True),)

    id: Mapped[str] = pk_column()
    organization_id: Mapped[str] = org_column()
    created_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The visible prefix, so a key can be identified in a list and revoked
    #: without anyone having to paste the secret back in to find it.
    prefix: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    role: Mapped[Role] = mapped_column(
        enum_column(Role, "role"), default=Role.PLANNER, nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Revoked rather than deleted. "This key was revoked on the 3rd" is a
    #: different and more useful answer than the key simply not being there.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class AuditEvent(Base):
    """Append-only. Who did what, to what, when.

    No update path and no delete path, by design: an audit trail that can be
    edited answers a different question from the one it was built for.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_org_at", "organization_id", "at"),
        Index("ix_audit_subject", "subject_type", "subject_id"),
    )

    id: Mapped[str] = pk_column()
    organization_id: Mapped[str] = org_column()
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: By id, not by name. Names change; the question stays answerable.
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    subject_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Structured detail. Never a password, never a key, never a full payload --
    #: an audit row that carries the secret it was recording the use of is a
    #: second copy of the secret.
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
