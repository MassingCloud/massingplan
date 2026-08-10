"""The schedule tables.

Shaped by the engine, not by the file formats: an ``Activity`` row carries the
things ``core.Task`` needs, and the reader-specific extras (activity codes,
UDFs, notes) live in JSON columns rather than a column per vendor field.

Five defects carried deliberately from the model this replaces, each fixed here:

* ``status`` and ``relationship type`` are enums, not bare strings. A typo in a
  string column is a silently-dropped relationship.
* ``remaining_days`` is a column, not derived from a percentage. Percent
  complete is a claim about work done; remaining duration is a claim about work
  left, and deriving one from the other assumes linear productivity.
* A baseline is **rows**, not a JSON blob. The blob could be stored and never
  queried, which is why baseline-to-baseline comparison was never built.
* No column named ``*_encrypted`` that nothing encrypts.
* Indexes are declared in ``__table_args__``, so ``create_all`` and Alembic
  produce the same schema on SQLite and Postgres alike.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.constraints import ConstraintType
from ..core.network import ActivityKind, LagCalendar, ProgressMode, RelationType
from .base import Base, TimestampMixin, date_column, days_column, org_column, pk_column


#: `native_enum=False` stores the enum's *value* as a VARCHAR with a CHECK
#: constraint. Native PostgreSQL enums need a migration to add a member, and the
#: engine's enums gain members -- an eleventh constraint type should be a code
#: change, not a database outage.
def _enum(enum_class: type, name: str) -> Enum:
    return Enum(
        enum_class,
        native_enum=False,
        length=32,
        name=name,
        values_callable=lambda e: [member.value for member in e],
    )


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[str] = pk_column()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (
        # Unique per organisation, not globally. A global unique code means one
        # tenant's naming collides with another's, and the error message tells
        # them a project they cannot see exists.
        UniqueConstraint("organization_id", "code", name="uq_project_org_code"),
        Index("ix_project_org_name", "organization_id", "name"),
    )

    id: Mapped[str] = pk_column()
    organization_id: Mapped[str] = org_column()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    #: The date the schedule is computed from. Nothing else means the same
    #: thing: the planned start is where the job was meant to begin.
    data_date: Mapped[date | None] = date_column()
    planned_start: Mapped[date | None] = date_column()
    must_finish_by: Mapped[date | None] = date_column()
    default_calendar_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    progress_mode: Mapped[ProgressMode] = mapped_column(
        _enum(ProgressMode, "progress_mode"), default=ProgressMode.RETAINED_LOGIC, nullable=False
    )
    lag_calendar: Mapped[LagCalendar] = mapped_column(
        _enum(LagCalendar, "lag_calendar"), default=LagCalendar.PREDECESSOR, nullable=False
    )
    source_format: Mapped[str] = mapped_column(String(16), default="", nullable=False)

    # `passive_deletes=True` throughout: the schema declares
    # `ondelete="CASCADE"`, so the database removes children itself. Without it
    # SQLAlchemy also issues a DELETE per child and then warns that it matched
    # nothing -- two mechanisms doing one job, one of them noisily.
    calendars: Mapped[list[Calendar]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )
    activities: Mapped[list[Activity]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )
    # Relationships hang off *two* activities as well as the project, so on a
    # project delete the database has already removed them via the activity
    # cascade by the time the ORM issues its own DELETE, and SQLAlchemy warns
    # that it matched nothing. `passive_deletes="all"` would silence that, but
    # it cannot coexist with `delete-orphan` -- which is what makes
    # `relationships_.clear()` work on re-import. Re-import is the common path;
    # the warning is confined to an explicit project delete and is benign.
    relationships_: Mapped[list[Relationship]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )
    resources: Mapped[list[Resource]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )
    baselines: Mapped[list[Baseline]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )

    @property
    def current_baseline(self) -> Baseline | None:
        return next((b for b in self.baselines if b.is_current), None)


class Calendar(Base):
    __tablename__ = "calendars"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_calendar_project_key"),)

    id: Mapped[str] = pk_column()
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The identifier the activities reference -- `5D`, `C1`, whatever the file
    #: called it. Separate from the surrogate key so an import can preserve it.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: `date.weekday()` numbering, Monday 0. A list rather than seven booleans:
    #: seven columns invite a query that checks six of them.
    working_weekdays: Mapped[list[int]] = mapped_column(JSON, default=lambda: [0, 1, 2, 3, 4])
    hours_per_day: Mapped[float] = mapped_column(Float, default=8.0, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    project: Mapped[Project] = relationship(back_populates="calendars")
    exceptions: Mapped[list[CalendarException]] = relationship(
        back_populates="calendar",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )


class CalendarException(Base):
    __tablename__ = "calendar_exceptions"
    __table_args__ = (UniqueConstraint("calendar_id", "day", name="uq_exception_calendar_day"),)

    id: Mapped[str] = pk_column()
    calendar_id: Mapped[str] = mapped_column(
        ForeignKey("calendars.id", ondelete="CASCADE"), index=True, nullable=False
    )
    day: Mapped[date] = mapped_column(nullable=False)
    #: True adds a working day (a make-up Saturday), False removes one. Both are
    #: exceptions; only one subtracts, and a boolean named `is_holiday` would
    #: make the make-up case unrepresentable.
    working: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    calendar: Mapped[Calendar] = relationship(back_populates="exceptions")


class Activity(Base, TimestampMixin):
    __tablename__ = "activities"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_activity_project_code"),
        # Sorting a Gantt and filtering a lookahead both hit these.
        Index("ix_activity_project_start", "project_id", "computed_start"),
        Index("ix_activity_project_critical", "project_id", "is_critical"),
    )

    id: Mapped[str] = pk_column()
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The planner's own identifier. It survives a re-baseline, which is why
    #: `compare` can match on it when the internal ids have all changed.
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    kind: Mapped[ActivityKind] = mapped_column(
        _enum(ActivityKind, "activity_kind"), default=ActivityKind.TASK, nullable=False
    )
    calendar_key: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    duration_days: Mapped[int] = days_column()
    #: A field, never a derivation. See the module docstring.
    remaining_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    percent_complete: Mapped[float | None] = mapped_column(Float, nullable=True)

    constraint: Mapped[ConstraintType] = mapped_column(
        _enum(ConstraintType, "constraint_type"), default=ConstraintType.NONE, nullable=False
    )
    constraint_date: Mapped[date | None] = date_column()
    actual_start: Mapped[date | None] = date_column()
    actual_finish: Mapped[date | None] = date_column()

    # -- computed, written back by the scheduler ---------------------------
    # Denormalised on purpose: "which activities are critical" is then an index
    # scan rather than a recalculation, and a Gantt page does not re-run CPM.
    computed_start: Mapped[date | None] = date_column()
    computed_finish: Mapped[date | None] = date_column()
    late_start: Mapped[date | None] = date_column()
    late_finish: Mapped[date | None] = date_column()
    #: Nullable, and null means *complete* -- not zero. A finished activity has
    #: no float; storing 0 would put history on the critical path.
    total_float_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    free_float_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_critical: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_longest_path: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    wbs_code: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    trade: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Vendor extras -- activity codes, UDFs. A JSON column rather than a column
    #: per vendor field, because the set is open and a round trip must not lose
    #: what it cannot name.
    extras: Mapped[dict] = mapped_column(JSON, default=dict)

    project: Mapped[Project] = relationship(back_populates="activities")


class Relationship(Base):
    __tablename__ = "relationships"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "predecessor_id", "successor_id", "type", name="uq_relationship"
        ),
        Index("ix_relationship_successor", "successor_id"),
    )

    id: Mapped[str] = pk_column()
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Cascade on both sides. Without it, deleting an activity leaves a
    # relationship pointing at nothing, and the next schedule run fails
    # validation on a project nobody knowingly broke.
    predecessor_id: Mapped[str] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    successor_id: Mapped[str] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[RelationType] = mapped_column(
        _enum(RelationType, "relation_type"), default=RelationType.FS, nullable=False
    )
    #: Signed. Negative is a lead, which is legal and which DCMA check 2 counts.
    lag_days: Mapped[int] = days_column()

    project: Mapped[Project] = relationship(back_populates="relationships_")


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_resource_project_key"),)

    id: Mapped[str] = pk_column()
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    type: Mapped[str] = mapped_column(String(32), default="labor", nullable=False)
    unit: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    #: How much of this resource exists per day. The leveller's cap.
    units_per_day: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    calendar_key: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    project: Mapped[Project] = relationship(back_populates="resources")
    assignments: Mapped[list[Assignment]] = relationship(
        back_populates="resource",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("activity_id", "resource_id", name="uq_assignment"),)

    id: Mapped[str] = pk_column()
    activity_id: Mapped[str] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Units per day held level across the activity's span -- not a total to be
    #: spread. Spreading makes peak demand a function of duration, which turns
    #: levelling non-monotonic. See `core/resources.py`.
    units_per_day: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    resource: Mapped[Resource] = relationship(back_populates="assignments")


class Baseline(Base, TimestampMixin):
    __tablename__ = "baselines"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_baseline_project_name"),
        Index("ix_baseline_current", "project_id", "is_current"),
    )

    id: Mapped[str] = pk_column()
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    data_date: Mapped[date | None] = date_column()
    project_finish: Mapped[date | None] = date_column()
    #: Exactly one per project, enforced in the service rather than by a partial
    #: index, because partial-unique syntax differs between SQLite and Postgres
    #: and a constraint that exists on only one of them is worse than none.
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    project: Mapped[Project] = relationship(back_populates="baselines")
    rows: Mapped[list[BaselineActivity]] = relationship(
        back_populates="baseline",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )


class BaselineActivity(Base):
    """One activity as it stood when the baseline was set.

    Rows, not a JSON blob. The blob in the system this replaces was stored and
    never queried, which is exactly why baseline-to-baseline comparison was
    never built there: you cannot join against a blob.
    """

    __tablename__ = "baseline_activities"
    __table_args__ = (UniqueConstraint("baseline_id", "code", name="uq_baseline_activity_code"),)

    id: Mapped[str] = pk_column()
    baseline_id: Mapped[str] = mapped_column(
        ForeignKey("baselines.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Matched on the planner's code, because a re-import renumbers the ids.
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    duration_days: Mapped[int] = days_column()
    start: Mapped[date | None] = date_column()
    finish: Mapped[date | None] = date_column()
    total_float_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_critical: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    baseline: Mapped[Baseline] = relationship(back_populates="rows")


class ImportJob(Base, TimestampMixin):
    """What a file import did, and everything it could not do exactly as asked.

    Kept because the issue log is the difference between an import that worked
    and one that looks like it worked. `has_logic=False` on a Primavera file
    means every activity will read as critical with zero float.
    """

    __tablename__ = "import_jobs"

    id: Mapped[str] = pk_column()
    organization_id: Mapped[str] = org_column()
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    filename: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    source_format: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    activity_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    relationship_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    has_logic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    issues: Mapped[list] = mapped_column(JSON, default=list)
