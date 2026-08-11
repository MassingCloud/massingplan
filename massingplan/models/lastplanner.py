"""Commitments, the constraints that block them, and the weeks they sit in.

Three tables, and the shape is the argument.

`weekly_plans` exists as a row rather than being derived from a date on each
commitment, because **the week is the thing that gets frozen**. PPC's
denominator is "every commitment made for this week", and if a week were only
an attribute of its commitments then removing a commitment would remove it from
the denominator -- which is the single commonest way the metric is gamed. A
plan row means a commitment is *in* a plan, and taking it out is an edit to a
plan somebody can see.

`lp_constraints` are rows on the commitment rather than a JSON blob, for the
reason `linear_quantities` are: the first question anybody asks a constraint
log is "what is procurement holding up across the whole job", and a blob cannot
answer it without a migration.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, pk_column

if TYPE_CHECKING:
    from .schedule import Project


class WeeklyPlanRow(Base, TimestampMixin):
    """One week's commitments, frozen together."""

    __tablename__ = "weekly_plans"
    __table_args__ = (
        UniqueConstraint("project_id", "week_starting", name="uq_weekly_plan_week"),
        Index("ix_weekly_plan_project_week", "project_id", "week_starting"),
    )

    id: Mapped[str] = pk_column()
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The Monday. Unique per project, because two plans for one week is two
    #: denominators for one PPC.
    week_starting: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    project: Mapped[Project] = relationship(back_populates="weekly_plans")
    commitments: Mapped[list[CommitmentRow]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
        order_by="CommitmentRow.sequence",
    )

    __mapper_args__ = {"confirm_deleted_rows": False}


class CommitmentRow(Base, TimestampMixin):
    """One promise. `completed` is nullable because "not yet assessed" is a
    third state, and storing it as `False` would report an unfinished week as a
    failed one.
    """

    __tablename__ = "lp_commitments"
    __table_args__ = (Index("ix_commitment_plan_sequence", "plan_id", "sequence"),)

    id: Mapped[str] = pk_column()
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("weekly_plans.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sequence: Mapped[int] = mapped_column(default=0, nullable=False)
    #: The activity this promise is against, by the planner's own code. Not a
    #: foreign key: a commitment can be made for work that has not been added
    #: to the CPM schedule yet, which is normal in a lookahead and is not a
    #: reason to refuse to record the promise.
    activity_code: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    description: Mapped[str] = mapped_column(String(400), nullable=False)
    crew: Mapped[str] = mapped_column(String(120), nullable=False)
    completed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: A `VarianceReason` value. Required by the service when `completed` is
    #: false; nullable here because the column is also used before assessment.
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    plan: Mapped[WeeklyPlanRow] = relationship(back_populates="commitments")
    constraints: Mapped[list[ConstraintRow]] = relationship(
        back_populates="commitment",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )

    __mapper_args__ = {"confirm_deleted_rows": False}


class ConstraintRow(Base, TimestampMixin):
    """Something blocking a commitment, with an owner and a date.

    Both required at the service layer: a constraint with no owner is not being
    removed by anybody, and one with no date is not being removed this week.
    """

    __tablename__ = "lp_constraints"

    id: Mapped[str] = pk_column()
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    commitment_id: Mapped[str] = mapped_column(
        ForeignKey("lp_commitments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: A `ConstraintKind` value.
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(String(400), nullable=False)
    owner: Mapped[str] = mapped_column(String(120), nullable=False)
    promised_by: Mapped[date] = mapped_column(Date, nullable=False)
    #: The day somebody said it was cleared. Null means still live -- and the
    #: engine reads a *future* removal date as still live today, so this is a
    #: date rather than a boolean on purpose.
    removed_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    commitment: Mapped[CommitmentRow] = relationship(back_populates="constraints")

    __mapper_args__ = {"confirm_deleted_rows": False}
