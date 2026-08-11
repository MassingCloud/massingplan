"""The location breakdown, and the trades that flow through it.

Separate from `schedule.py` because it answers a different question. That file
models *what the work is*; this one models *where it happens and who moves
through it*, which is the input to `core.locations` and has no meaning to CPM.

Three decisions worth naming.

**Quantities are rows, not a JSON blob on the trade.** The same argument the
baseline tables were built on: a blob can be stored and never queried, so the
first feature that needs "which trades have a quantity on level 7" cannot be
written without a migration. Rows also give the quantity a real foreign key to
its location, so renaming a level cannot orphan it.

**`sequence` is explicit on both tables.** Location order *is* the direction of
flow and trade order *is* the handover sequence -- neither is decoration, and
both get edited. A level inserted at 3 must not silently renumber everything
above it, which is exactly what relying on a list index would do.

**A trade carries either a flat duration or a rate, and both are stored.** A
planner without a take-off says "four days a floor"; one with a take-off says
"380 m2 at 95 m2 a day". Keeping both means switching from one to the other is
an edit rather than a re-entry, and `core.locations` already prefers the rate
when a quantity exists for that location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, org_column, pk_column

if TYPE_CHECKING:  # pragma: no cover - `schedule` imports this module, so a
    # runtime import would be circular. SQLAlchemy resolves the string
    # "Project" through its own registry at mapper-configuration time.
    from .schedule import Project


class ProjectLocation(Base, TimestampMixin):
    """One place work happens -- a level, a zone, a chainage.

    Named `ProjectLocation` rather than `Location` because `core.locations`
    already owns that name for the engine's own value type, and one of these is
    a database row while the other is an input to a calculation. Two things
    called `Location` in one import list is a bug waiting for a tired reader.
    """

    __tablename__ = "locations"
    __table_args__ = (
        # The planner's own key, unique within the project. Trades reference it,
        # and a duplicate would make "which level is this" unanswerable.
        UniqueConstraint("project_id", "key", name="uq_location_project_key"),
        Index("ix_location_project_sequence", "project_id", "sequence"),
    )

    id: Mapped[str] = pk_column()
    organization_id: Mapped[str] = org_column()
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: Direction of flow. Explicit, not the row order -- see the module docstring.
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    project: Mapped[Project] = relationship(back_populates="locations")


class LinearActivity(Base, TimestampMixin):
    """One trade, flowing through every location in order.

    Not an `Activity`. A CPM activity happens once; this happens once *per
    location* and is expanded into one activity each by `core.locations`. Giving
    them the same table would mean either a nullable location on every activity
    or a flag nobody sets consistently.
    """

    __tablename__ = "linear_activities"
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_linear_project_key"),
        Index("ix_linear_project_sequence", "project_id", "sequence"),
    )

    id: Mapped[str] = pk_column()
    organization_id: Mapped[str] = org_column()
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: Handover order: each trade follows the one before it through every
    #: location. This is the sequence, not a priority.
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Used where there is no quantity and rate for a location.
    duration_days: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Units per working day for one crew. Null means "no take-off, use the
    #: flat duration" -- which is a different statement from a rate of zero, and
    #: `core.locations` refuses zero rather than dividing by it.
    rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Working days the following trade must wait after this one clears a
    #: location. Negative is a deliberate overlap and is reported as an issue.
    buffer_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    crews: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    calendar_key: Mapped[str] = mapped_column(String(32), default="STD", nullable=False)

    project: Mapped[Project] = relationship(back_populates="linear_activities")
    quantities: Mapped[list[LinearQuantity]] = relationship(
        back_populates="activity",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )


class LinearQuantity(Base):
    """How much of one trade's work sits in one location.

    A row rather than a key in a JSON blob on the trade, so it has a real
    foreign key to the location it belongs to and can be queried, edited and
    cascaded like anything else.
    """

    __tablename__ = "linear_quantities"
    __table_args__ = (UniqueConstraint("activity_id", "location_id", name="uq_linear_quantity"),)

    id: Mapped[str] = pk_column()
    activity_id: Mapped[str] = mapped_column(
        ForeignKey("linear_activities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    location_id: Mapped[str] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    activity: Mapped[LinearActivity] = relationship(back_populates="quantities")
    # Deliberately left lazy. The trades page names the location of every stored
    # quantity, which looks like an N+1 waiting to happen -- but `Project`
    # loads `locations` with `selectin`, so every row this could point at is
    # already in the identity map and the many-to-one resolves without a query.
    # Measured: 12 statements to render the page this way, 13 with `selectin`
    # here. The property that matters is guarded by a query count in
    # `tests/test_takeoff.py`, not by this line.
    location: Mapped[ProjectLocation] = relationship()

    # The database cascades from both parents, so the unit of work's own DELETE
    # matches nothing and warns. Same reasoning as `Relationship` in schedule.py.
    __mapper_args__ = {"confirm_deleted_rows": False}
