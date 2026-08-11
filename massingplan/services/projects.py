"""Project operations: import, reschedule, baseline, compare.

The layer where a stored project becomes an engine run and back again. Web
routes and the CLI both call these; neither reimplements the sequence, because
"schedule then write back then assess" done twice is two places for the write
back to be forgotten.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from ..core import health
from ..core.compare import MatchKey, compare
from ..core.model import ExchangeSchedule
from ..core.schedule import ScheduleOutcome, schedule_network
from ..models import Baseline, ImportJob, Project
from . import repository as repo


class ProjectError(RuntimeError):
    """Something about this project's data prevents the operation."""


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").upper()
    return cleaned[:60] or "PROJECT"


def unique_code(session: Session, organization_id: str, wanted: str) -> str:
    """A project code free within this organisation.

    Suffixed rather than rejected: somebody importing the second revision of a
    file should not have to invent a name for it.
    """
    base = _slug(wanted)
    existing = {
        p.code for p in repo.list_projects(session, organization_id) if p.code.startswith(base)
    }
    if base not in existing:
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if candidate not in existing:
            return candidate
    raise ProjectError(f"cannot find a free project code starting {base}")


def import_schedule(
    session: Session,
    schedule: ExchangeSchedule,
    *,
    organization_id: str,
    name: str,
    filename: str = "",
    project: Project | None = None,
) -> tuple[Project, ScheduleOutcome, ImportJob]:
    """Store an imported schedule, compute it, and record what the import cost.

    The ``ImportJob`` is not bookkeeping. ``has_logic=False`` means the file
    arrived with no relationships, and a network with no relationships has no
    critical path -- every activity reads as critical with zero float while the
    import reports success. That has to be visible after the fact, not only in
    the response the uploader happened to see.
    """
    problems = schedule.validate()
    if problems:
        raise ProjectError(
            f"the schedule cannot be stored: {len(problems)} problems -- " + "; ".join(problems[:5])
        )

    code = project.code if project else unique_code(session, organization_id, name)
    project = repo.save_schedule(
        session, schedule, organization_id=organization_id, name=name, code=code, project=project
    )
    outcome = reschedule(session, project)

    job = ImportJob(
        organization_id=organization_id,
        project_id=project.id,
        filename=filename,
        source_format=schedule.source_format,
        activity_count=len(schedule.activities),
        relationship_count=len(schedule.relationships),
        has_logic=bool(schedule.relationships),
        issues=schedule.issues.to_list(),
    )
    session.add(job)
    session.flush()
    return project, outcome, job


def reschedule_with_links(session: Session, project: Project) -> tuple[ScheduleOutcome, list[Any]]:
    """Run the engine, store the answer, and hand back the links it used.

    The project page needs both: the outcome for the dates and the links to draw
    dependency arrows. Calling `reschedule()` and then `repo.to_network()` again
    rebuilt every Task, Link and WorkCalendar a second time per page view -- on
    a two-thousand-activity project, a full second conversion of data already in
    hand.
    """
    tasks, links, calendars, options = repo.to_network(project)
    if not tasks:
        raise ProjectError("this project has no activities to schedule")
    outcome = schedule_network(
        tasks, links, calendars, data_date=project.data_date, options=options
    )
    repo.write_back(session, project, outcome)
    return outcome, list(links)


def reschedule(session: Session, project: Project) -> ScheduleOutcome:
    """Run the engine and store the answer.

    Always both. A schedule computed and not written back is why a Gantt page
    ends up recalculating on every render, and why two pages can disagree.
    """
    outcome, _links = reschedule_with_links(session, project)
    return outcome


def assess(
    session: Session, project: Project, outcome: ScheduleOutcome | None = None
) -> dict[str, Any]:
    """DCMA, with checks 11 and 14 turned on when a baseline exists.

    Those two need baseline finish dates. Before persistence they could not run
    at all -- which is the concrete reason a schedule could never score above
    10 runnable checks in this product.
    """
    outcome = outcome or reschedule(session, project)
    tasks, links, calendars, _options = repo.to_network(project)
    baseline = project.current_baseline
    finishes = repo.baseline_finish_dates(project, baseline) if baseline else None
    resourced = [a.activity_id for r in project.resources for a in r.assignments] or None

    report = health.assess(
        outcome,
        tasks,
        links,
        calendars,
        baseline_finish=finishes or None,
        resourced_activity_ids=resourced,
    )
    return report.to_dict()


def set_baseline(
    session: Session,
    project: Project,
    *,
    name: str,
    notes: str = "",
    outcome: ScheduleOutcome | None = None,
) -> Baseline:
    outcome = outcome or reschedule(session, project)
    if any(b.name == name for b in project.baselines):
        raise ProjectError(f"this project already has a baseline named {name!r}")
    return repo.set_baseline(session, project, outcome, name=name, notes=notes)


def compare_to_baseline(
    session: Session,
    project: Project,
    baseline: Baseline,
    *,
    outcome: ScheduleOutcome | None = None,
) -> dict[str, Any]:
    """Delay attribution against a stored baseline.

    Matched on the planner's activity **code**, not the row id. A re-import
    renumbers every id, so matching on them reports the whole schedule as
    removed and re-added -- technically correct and completely useless.
    """
    outcome = outcome or reschedule(session, project)
    tasks, links, calendars, options = repo.to_network(project)

    # The baseline is replayed as a network of its own so the comparison has two
    # real schedules rather than one schedule and a table of dates. Its
    # activities are keyed by code, which is what makes them matchable.
    from ..core.network import Link, Task

    baseline_tasks = [
        Task(
            id=row.code,
            name=row.name,
            duration_days=row.duration_days,
            calendar_id=next(iter(calendars)),
        )
        for row in baseline.rows
    ]
    code_of = {a.id: a.code for a in project.activities}
    known = {t.id for t in baseline_tasks}
    baseline_links = [
        Link(code_of[link.predecessor], code_of[link.successor], link.type, link.lag_days)
        for link in links
        if code_of.get(link.predecessor) in known and code_of.get(link.successor) in known
    ]
    if not baseline_tasks:
        raise ProjectError(f"baseline {baseline.name!r} has no activities to compare against")

    baseline_outcome = schedule_network(
        baseline_tasks,
        baseline_links,
        calendars,
        data_date=baseline.data_date or project.data_date,
        options=options,
    )
    current_tasks = [
        Task(
            id=code_of.get(t.id, t.id),
            name=t.name,
            duration_days=t.duration_days,
            calendar_id=t.calendar_id,
            kind=t.kind,
            constraint=t.constraint,
            constraint_date=t.constraint_date,
            actual_start=t.actual_start,
            actual_finish=t.actual_finish,
            remaining_days=t.remaining_days,
        )
        for t in tasks
    ]
    current_links = [
        Link(
            code_of.get(link.predecessor, link.predecessor),
            code_of.get(link.successor, link.successor),
            link.type,
            link.lag_days,
        )
        for link in links
    ]
    current_outcome = schedule_network(
        current_tasks,
        current_links,
        calendars,
        data_date=project.data_date,
        options=options,
    )

    result = compare(
        baseline_outcome,
        current_outcome,
        baseline_network=(baseline_tasks, baseline_links),
        current_network=(current_tasks, current_links),
        match=MatchKey.ID,
    )
    return result.to_dict()


def summary(project: Project, outcome: ScheduleOutcome) -> dict[str, Any]:
    """The headline a project card shows, from a freshly computed schedule."""
    baseline = project.current_baseline
    slip = None
    if baseline and baseline.project_finish:
        slip = (outcome.project_finish - baseline.project_finish).days
    return {
        "id": project.id,
        "code": project.code,
        "name": project.name,
        "data_date": project.data_date.isoformat() if project.data_date else None,
        "project_start": outcome.project_start.isoformat(),
        "project_finish": outcome.project_finish.isoformat(),
        "duration_working_days": outcome.duration_working_days,
        "activity_count": len(outcome.dates),
        "critical_count": sum(1 for d in outcome.dates.values() if d.is_critical),
        "baseline": baseline.name if baseline else None,
        # Signed days against the current baseline, or None when there is no
        # baseline -- which is different from "on time" and must not render as 0.
        "slip_days": slip,
        "stale": False,
    }


def stored_summary(project: Project) -> dict[str, Any]:
    """The same headline, read from the denormalised columns. No CPM run.

    This is what the columns were denormalised *for*. Rescheduling every project
    to render a list means a full forward and backward pass, plus a write-back,
    per row -- on fifty projects that is fifty CPM runs and fifty transactions
    to draw a table nobody has clicked into yet, and it degrades linearly with
    the thing a growing customer has more of.

    `stale` is true when a project has never been scheduled, so the page can say
    "not computed yet" rather than printing a blank date that reads as a bug.
    """
    # Every value here is a column on `projects`. Nothing below touches a
    # relationship, which is the point: `repository.list_projects` loads the
    # rows with the children suppressed, so a list of twenty projects is one
    # query and twenty objects rather than twenty thousand.
    finish = project.computed_finish
    baseline_name = project.baseline_name
    baseline_finish = project.baseline_finish
    slip = None
    if baseline_finish and finish:
        slip = (finish - baseline_finish).days
    return {
        "id": project.id,
        "code": project.code,
        "name": project.name,
        "data_date": project.data_date.isoformat() if project.data_date else None,
        "project_start": project.computed_start.isoformat() if project.computed_start else None,
        "project_finish": finish.isoformat() if finish else None,
        "duration_working_days": None,
        "activity_count": project.activity_count,
        "critical_count": project.critical_count,
        "baseline": baseline_name or None,
        # Signed days against the current baseline, or None when there is no
        # baseline -- which is different from "on time" and must not render as 0.
        "slip_days": slip,
        "stale": finish is None,
    }


def linear_schedule(project: Project, *, start: date | None = None) -> dict[str, Any] | None:
    """The stored location model, computed. `None` when there is not one.

    `None` rather than an empty result, because "this project has no location
    breakdown" and "this project has a breakdown that schedules to nothing" are
    different states and the page says something different for each.

    Returns the same shape `api.schedules.schedule_linear` does, so the template
    that renders the worked example renders a real project unchanged.
    """
    from ..core.locations import LinearScheduleError, compute, to_network
    from ..core.timeaxis import standard_calendar

    tasks, locations = repo.to_linear(project)
    if not tasks or not locations:
        return None

    began = start or project.planned_start or project.data_date or date.today()
    calendar = standard_calendar()
    try:
        result = compute(tasks, locations)
        net_tasks, links, _cals = to_network(
            result, tasks, locations, start=began, calendar=calendar
        )
    except LinearScheduleError as exc:
        raise ProjectError(str(exc)) from exc

    return {
        "start": began.isoformat(),
        "duration_working_days": result.duration_days,
        "segments": result.to_rows(start=began, calendar=calendar),
        "interferences": [i.to_dict() for i in result.interferences],
        "continuity_cost_days": result.continuity_cost_days,
        "issues": result.issues.to_list(),
        "activities": [
            {
                "id": task.id,
                "name": task.name,
                "duration_days": task.duration_days,
                "calendar_id": task.calendar_id,
                "constraint": task.constraint.value,
                "constraint_date": (
                    task.constraint_date.isoformat() if task.constraint_date else None
                ),
                "predecessors": [
                    {"id": link.predecessor, "type": link.type.value, "lag_days": link.lag_days}
                    for link in links
                    if link.successor == task.id
                ],
            }
            for task in net_tasks
        ],
    }
