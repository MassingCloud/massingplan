"""The bridge between stored rows and the engine's types.

One direction each way, in one place. Scatter this and the two ends drift: a
field gets added to the model, the loader learns about it, the saver does not,
and a round trip through the database quietly loses it.

Tenancy lives here too. ``scoped()`` returns a pre-filtered select and **fails
closed** when no organisation is active -- an impossible filter rather than
every row. The alternative, remembering to add `.where(organization_id == ...)`
at each call site, is one forgotten clause away from a data breach.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from ..core.model import (
    Calendar as ExchangeCalendar,
)
from ..core.model import (
    CalendarException as ExchangeCalendarException,
)
from ..core.model import (
    ExchangeActivity,
    ExchangeAssignment,
    ExchangeRelationship,
    ExchangeResource,
    ExchangeSchedule,
)
from ..core.network import Link, SchedulerOptions, Task
from ..core.resources import Demand, ResourceAvailability
from ..core.schedule import ScheduleOutcome
from ..core.timeaxis import WorkCalendar
from ..models import (
    Activity,
    Assignment,
    Baseline,
    BaselineActivity,
    Calendar,
    CalendarException,
    LinearActivity,
    LinearQuantity,
    Organization,
    Project,
    ProjectLocation,
    Relationship,
    Resource,
)

T = TypeVar("T")

#: The organisation every row belongs to until auth lands. A constant rather
#: than "the first row found", so two installs of the same version agree and a
#: seeded fixture is reproducible.
DEFAULT_ORG_ID = "00000000000000000000000000000001"
DEFAULT_ORG_SLUG = "default"


class NoActiveOrganizationError(RuntimeError):
    """A scoped query was built with no organisation. Returns nothing, by design."""


def ensure_default_organization(session: Session) -> Organization:
    org = session.get(Organization, DEFAULT_ORG_ID)
    if org is None:
        org = Organization(id=DEFAULT_ORG_ID, name="Default", slug=DEFAULT_ORG_SLUG)
        session.add(org)
        session.flush()
    return org


def scoped(model: type[T], organization_id: str | None) -> Select[tuple[T]]:
    """A select already filtered to one organisation.

    With no organisation the filter is impossible rather than absent. A query
    that returns nothing is a visible bug; one that returns everything is a
    breach that looks like a working feature.
    """
    statement = select(model)
    if not organization_id:
        return statement.where(model.organization_id.is_(None)).where(  # type: ignore[attr-defined]
            model.organization_id.is_not(None)  # type: ignore[attr-defined]
        )
    return statement.where(model.organization_id == organization_id)  # type: ignore[attr-defined]


def get_project(session: Session, project_id: str, organization_id: str | None) -> Project | None:
    """A project, or ``None`` -- including when it belongs to somebody else.

    The caller turns ``None`` into a 404, never a 403. "This exists but is not
    yours" tells one contractor that another contractor's project id is real.
    """
    return session.scalars(scoped(Project, organization_id).where(Project.id == project_id)).first()


def list_projects(session: Session, organization_id: str | None) -> Sequence[Project]:
    """Projects with **no children loaded**, for the list page.

    Every relationship on `Project` is `lazy="selectin"`, which is right for a
    project page and wrong for a list of them: loading twenty projects would
    otherwise pull every activity, calendar, relationship and baseline of all
    twenty -- twenty thousand ORM objects to draw twenty table rows.

    `noload` suppresses that. It is safe because `projects.stored_summary` reads
    only columns on `projects`, and a caller that wants the children should use
    `session.get(Project, id)`, which loads them as before.
    """
    from sqlalchemy.orm import noload

    statement = (
        scoped(Project, organization_id)
        .order_by(Project.updated_at.desc())
        .options(
            noload(Project.activities),
            noload(Project.calendars),
            noload(Project.relationships_),
            noload(Project.baselines),
            noload(Project.resources),
            noload(Project.locations),
            noload(Project.linear_activities),
        )
    )
    return session.scalars(statement).all()


# -- store -----------------------------------------------------------------


def save_schedule(
    session: Session,
    schedule: ExchangeSchedule,
    *,
    organization_id: str,
    name: str,
    code: str,
    project: Project | None = None,
) -> Project:
    """Persist a hub-model schedule, replacing the project's contents.

    Replace rather than merge, deliberately. A re-import is a new statement of
    what the schedule is; merging it against what was there produces a hybrid
    nobody authored and nobody can reproduce from either input. Baselines
    survive -- they are the record of what it *was*, and are matched by activity
    code rather than by row identity for exactly this reason.
    """
    if project is None:
        project = Project(organization_id=organization_id, name=name, code=code)
        session.add(project)
        session.flush()
    else:
        project.name = name
        project.code = code
        # `clear()` on a cascade="all, delete-orphan" collection issues the
        # deletes; assigning a new list would orphan the old rows instead.
        project.calendars.clear()
        project.activities.clear()
        project.relationships_.clear()
        project.resources.clear()
        session.flush()

    project.data_date = schedule.data_date
    project.planned_start = schedule.planned_start
    project.must_finish_by = schedule.must_finish_by
    project.source_format = schedule.source_format
    project.progress_mode = schedule.options.progress_mode
    project.lag_calendar = schedule.options.lag_calendar

    default_calendar = schedule.default_calendar()
    project.default_calendar_id = default_calendar.id if default_calendar else None

    for cal_source in schedule.calendars:
        calendar = Calendar(
            project_id=project.id,
            key=cal_source.id,
            name=cal_source.name,
            working_weekdays=sorted(cal_source.working_weekdays),
            hours_per_day=cal_source.hours_per_day,
            is_default=cal_source.is_default,
        )
        calendar.exceptions = [
            CalendarException(day=e.day, working=e.working, name=e.name)
            for e in cal_source.exceptions
        ]
        project.calendars.append(calendar)

    by_code: dict[str, Activity] = {}
    for act_source in schedule.activities:
        activity = Activity(
            project_id=project.id,
            code=act_source.code or act_source.id,
            name=act_source.name,
            kind=act_source.kind,
            calendar_key=act_source.calendar_id
            or (default_calendar.id if default_calendar else ""),
            duration_days=act_source.duration_days,
            remaining_days=act_source.remaining_duration_days,
            percent_complete=act_source.percent_complete,
            constraint=act_source.constraint,
            constraint_date=act_source.constraint_date,
            actual_start=act_source.actual_start,
            actual_finish=act_source.actual_finish,
            wbs_code=act_source.code,
            trade=act_source.trade,
            notes=act_source.notes,
            extras={"activity_codes": act_source.activity_codes, "source_id": act_source.id},
        )
        project.activities.append(activity)
        by_code[act_source.id] = activity
    session.flush()

    for rel_source in schedule.relationships:
        predecessor = by_code.get(rel_source.predecessor_id)
        successor = by_code.get(rel_source.successor_id)
        if predecessor is None or successor is None:
            continue
        project.relationships_.append(
            Relationship(
                project_id=project.id,
                predecessor_id=predecessor.id,
                successor_id=successor.id,
                type=rel_source.type,
                lag_days=rel_source.lag_days,
            )
        )

    resources_by_key: dict[str, Resource] = {}
    for res_source in schedule.resources:
        resource = Resource(
            project_id=project.id,
            key=res_source.id,
            name=res_source.name,
            type=res_source.type,
            unit=res_source.unit,
            units_per_day=res_source.max_units_per_day or 1.0,
            calendar_key=res_source.calendar_id or "",
        )
        project.resources.append(resource)
        resources_by_key[res_source.id] = resource
    session.flush()

    for asg_source in schedule.assignments:
        assigned_activity = by_code.get(asg_source.activity_id)
        assigned_resource = resources_by_key.get(asg_source.resource_id)
        if assigned_activity is None or assigned_resource is None:
            continue
        session.add(
            Assignment(
                activity_id=assigned_activity.id,
                resource_id=assigned_resource.id,
                units_per_day=asg_source.units_per_day or 1.0,
            )
        )

    session.flush()
    return project


def write_back(session: Session, project: Project, outcome: ScheduleOutcome) -> None:
    """Store the computed dates so a Gantt page is a read, not a recalculation."""
    by_id = {a.id: a for a in project.activities}
    on_path = set(outcome.longest_path)
    for activity_id, dates in outcome.dates.items():
        activity = by_id.get(activity_id)
        if activity is None:
            continue
        activity.computed_start = dates.start
        activity.computed_finish = dates.finish
        activity.late_start = dates.late_start
        activity.late_finish = dates.late_finish
        # `None` stays `None`: a completed activity has no float, and writing 0
        # would put finished work on the critical path.
        activity.total_float_days = dates.total_float_days
        activity.free_float_days = dates.free_float_days
        activity.is_critical = dates.is_critical
        activity.is_longest_path = activity_id in on_path

    # The list-page headline, written here because this is the only place a
    # computed schedule is persisted -- so the summary cannot drift from the
    # activity rows it summarises.
    project.computed_start = outcome.project_start
    project.computed_finish = outcome.project_finish
    project.activity_count = len(outcome.dates)
    project.critical_count = sum(1 for d in outcome.dates.values() if d.is_critical)
    session.flush()


# -- load ------------------------------------------------------------------


def to_network(
    project: Project,
) -> tuple[list[Task], list[Link], dict[str, WorkCalendar], SchedulerOptions]:
    """The engine's inputs, straight from stored rows."""
    calendars: dict[str, WorkCalendar] = {}
    for row in project.calendars:
        calendars[row.key] = ExchangeCalendar(
            id=row.key,
            name=row.name,
            working_weekdays=set(row.working_weekdays or [0, 1, 2, 3, 4]),
            exceptions=[
                ExchangeCalendarException(day=e.day, working=e.working, name=e.name)
                for e in row.exceptions
            ],
            hours_per_day=row.hours_per_day,
            is_default=row.is_default,
        ).to_work_calendar()
    if not calendars:
        from ..core.timeaxis import standard_calendar

        calendars["STD"] = standard_calendar()
    fallback = project.default_calendar_id or next(iter(calendars))

    tasks = [
        Task(
            id=row.id,
            name=row.name or row.code,
            duration_days=row.duration_days,
            calendar_id=row.calendar_key if row.calendar_key in calendars else fallback,
            kind=row.kind,
            constraint=row.constraint,
            constraint_date=row.constraint_date,
            actual_start=row.actual_start,
            actual_finish=row.actual_finish,
            remaining_days=row.remaining_days,
            percent_complete=row.percent_complete,
        )
        for row in project.activities
        if not row.kind.is_derived
    ]
    scheduled = {t.id for t in tasks}
    links = [
        Link(r.predecessor_id, r.successor_id, r.type, r.lag_days)
        for r in project.relationships_
        if r.predecessor_id in scheduled and r.successor_id in scheduled
    ]
    options = SchedulerOptions(
        progress_mode=project.progress_mode,
        lag_calendar=project.lag_calendar,
        must_finish_by=project.must_finish_by,
    )
    return tasks, links, calendars, options


def to_exchange(project: Project) -> ExchangeSchedule:
    """The hub model, for export back out to XER or MSPDI."""
    schedule = ExchangeSchedule(
        project_id=project.code,
        project_name=project.name,
        data_date=project.data_date,
        planned_start=project.planned_start,
        must_finish_by=project.must_finish_by,
        default_calendar_id=project.default_calendar_id,
        source_format=project.source_format,
    )
    schedule.calendars = [
        ExchangeCalendar(
            id=c.key,
            name=c.name,
            working_weekdays=set(c.working_weekdays or [0, 1, 2, 3, 4]),
            exceptions=[
                ExchangeCalendarException(day=e.day, working=e.working, name=e.name)
                for e in c.exceptions
            ],
            hours_per_day=c.hours_per_day,
            is_default=c.is_default,
        )
        for c in project.calendars
    ]
    schedule.activities = [
        ExchangeActivity(
            id=a.id,
            name=a.name,
            kind=a.kind,
            calendar_id=a.calendar_key,
            duration_days=a.duration_days,
            remaining_duration_days=a.remaining_days,
            early_start=a.computed_start,
            early_finish=a.computed_finish,
            late_start=a.late_start,
            late_finish=a.late_finish,
            actual_start=a.actual_start,
            actual_finish=a.actual_finish,
            constraint=a.constraint,
            constraint_date=a.constraint_date,
            total_float_days=a.total_float_days,
            free_float_days=a.free_float_days,
            is_longest_path=a.is_longest_path,
            code=a.code,
            trade=a.trade,
            notes=a.notes,
            activity_codes=(a.extras or {}).get("activity_codes", {}),
        )
        for a in project.activities
    ]
    schedule.relationships = [
        ExchangeRelationship(r.predecessor_id, r.successor_id, r.type, r.lag_days)
        for r in project.relationships_
    ]
    schedule.resources = [
        ExchangeResource(
            id=r.key,
            name=r.name,
            type=r.type,
            unit=r.unit,
            max_units_per_day=r.units_per_day,
            calendar_id=r.calendar_key or None,
        )
        for r in project.resources
    ]
    schedule.assignments = [
        ExchangeAssignment(
            activity_id=a.activity_id, resource_id=r.key, units_per_day=a.units_per_day
        )
        for r in project.resources
        for a in r.assignments
    ]
    return schedule


def resource_inputs(
    project: Project,
) -> tuple[list[Demand], list[ResourceAvailability]]:
    """Demands and caps for the leveller."""
    demands = [
        Demand(a.activity_id, r.key, a.units_per_day)
        for r in project.resources
        for a in r.assignments
    ]
    availability = [
        ResourceAvailability(r.key, r.units_per_day, calendar_id=r.calendar_key or None)
        for r in project.resources
    ]
    return demands, availability


# -- baselines -------------------------------------------------------------


def set_baseline(
    session: Session,
    project: Project,
    outcome: ScheduleOutcome,
    *,
    name: str,
    notes: str = "",
    make_current: bool = True,
) -> Baseline:
    """Freeze the computed schedule as a named baseline."""
    if make_current:
        # Exactly one current baseline, enforced here rather than by a partial
        # unique index: that syntax differs between SQLite and Postgres, and a
        # constraint present on only one of them is worse than none.
        for existing in project.baselines:
            existing.is_current = False

    baseline = Baseline(
        project_id=project.id,
        name=name,
        notes=notes,
        data_date=outcome.data_date,
        project_finish=outcome.project_finish,
        is_current=make_current,
    )
    by_id = {a.id: a for a in project.activities}
    for activity_id, dates in outcome.dates.items():
        activity = by_id.get(activity_id)
        if activity is None:
            continue
        baseline.rows.append(
            BaselineActivity(
                code=activity.code,
                name=activity.name,
                duration_days=dates.duration_days,
                start=dates.start,
                finish=dates.finish,
                total_float_days=dates.total_float_days,
                is_critical=dates.is_critical,
            )
        )
    project.baselines.append(baseline)
    if make_current:
        # Denormalised onto the project so the list page can show slip without
        # loading every baseline of every project. Written only here, where the
        # current baseline is chosen.
        project.baseline_name = name
        project.baseline_finish = outcome.project_finish
    session.flush()
    return baseline


def baseline_finish_dates(project: Project, baseline: Baseline) -> dict[str, date]:
    """``{activity_id: baseline finish}`` -- what DCMA checks 11 and 14 need.

    Keyed by the *current* activity id but matched on the planner's code, since
    a re-import renumbers the ids and matching on them would report every
    activity as new and every baseline row as deleted.
    """
    by_code = {a.code: a.id for a in project.activities}
    return {
        by_code[row.code]: row.finish
        for row in baseline.rows
        if row.code in by_code and row.finish is not None
    }


def baseline_as_outcome_rows(baseline: Baseline) -> list[dict[str, Any]]:
    """The baseline in the row shape the comparison and the UI already read."""
    return [
        {
            "activity_id": row.code,
            "start": row.start.isoformat() if row.start else None,
            "finish": row.finish.isoformat() if row.finish else None,
            "duration_days": row.duration_days,
            "total_float_days": row.total_float_days,
            "is_critical": row.is_critical,
        }
        for row in baseline.rows
    ]


# -- the location model ----------------------------------------------------


def to_linear(project: Project) -> tuple[list[Any], list[Any]]:
    """Stored rows to `core.locations` inputs.

    Returns `(tasks, locations)` in that order because `core.locations.compute`
    takes them that way, and swapping two same-shaped lists at a call site is
    the kind of mistake types do not catch.

    Quantities are keyed by the location's **key**, not its row id, because that
    is what `LinearTask.quantities` matches against and what the planner typed.
    """
    from ..core.locations import LinearTask, Location

    locations = [
        Location(id=row.key, name=row.name, sequence=row.sequence)
        for row in sorted(project.locations, key=lambda location: (location.sequence, location.key))
    ]
    by_id = {row.id: row.key for row in project.locations}

    tasks = []
    for row in sorted(project.linear_activities, key=lambda a: (a.sequence, a.key)):
        quantities = {
            by_id[q.location_id]: q.quantity for q in row.quantities if q.location_id in by_id
        }
        tasks.append(
            LinearTask(
                id=row.key,
                name=row.name,
                duration_days=row.duration_days,
                quantities=quantities,
                rate=row.rate,
                buffer_days=row.buffer_days,
                crews=row.crews,
                calendar_id=row.calendar_key,
            )
        )
    return tasks, locations


def replace_locations(
    session: Session, project: Project, entries: Sequence[tuple[str, str]]
) -> None:
    """Set the whole breakdown at once, in the order given.

    Replace rather than merge, for the reason a re-import replaces a schedule: a
    breakdown is a statement of what the building is, and merging two of them
    produces a floor list nobody authored.

    Quantities cascade away with the locations they belonged to. That is the
    honest outcome -- a quantity against a level that no longer exists is not
    data worth keeping -- and it is why they are rows with a foreign key rather
    than keys in a blob, which would have silently survived.
    """
    project.locations.clear()
    session.flush()
    for index, (key, name) in enumerate(entries):
        project.locations.append(
            ProjectLocation(
                organization_id=project.organization_id,
                project_id=project.id,
                key=key,
                name=name,
                sequence=index,
            )
        )
    session.flush()


def upsert_linear_activity(
    session: Session,
    project: Project,
    *,
    key: str,
    name: str = "",
    duration_days: int = 1,
    rate: float | None = None,
    buffer_days: int = 0,
    crews: int = 1,
    calendar_key: str = "STD",
    quantities: dict[str, float] | None = None,
) -> LinearActivity:
    """Add or update one trade, keyed by the planner's own code.

    Upsert rather than append: re-entering a trade should correct it, not
    produce a second one with the same name three rows down.
    """
    existing = next((a for a in project.linear_activities if a.key == key), None)
    if existing is None:
        existing = LinearActivity(
            organization_id=project.organization_id,
            project_id=project.id,
            key=key,
            sequence=len(project.linear_activities),
        )
        project.linear_activities.append(existing)

    existing.name = name or key
    existing.duration_days = duration_days
    existing.rate = rate
    existing.buffer_days = buffer_days
    existing.crews = crews
    existing.calendar_key = calendar_key

    if quantities is not None:
        by_key = {row.key: row.id for row in project.locations}
        existing.quantities.clear()
        session.flush()
        for location_key, amount in quantities.items():
            if location_key in by_key:
                existing.quantities.append(
                    LinearQuantity(location_id=by_key[location_key], quantity=amount)
                )
    session.flush()
    return existing
