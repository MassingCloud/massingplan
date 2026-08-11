"""Stored weekly plans to `core.lastplanner` and back.

The bridge, and it is deliberately thin. Every rule that makes PPC mean
something -- the frozen denominator, the tri-state assessment, the required
reason, the refusal to commit constrained work -- lives in `core.lastplanner`
and is enforced when the rows are turned into engine objects. Re-implementing
any of it here would give two answers to "is this week measurable", and the one
the page happened to call would win.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..core.lastplanner import (
    Commitment,
    Constraint,
    ConstraintKind,
    LastPlannerError,
    VarianceReason,
    WeeklyPlan,
    assess,
    commit,
)
from ..models import CommitmentRow, ConstraintRow, Project, WeeklyPlanRow


def monday_of(day: date) -> date:
    """The Monday of the week containing `day`.

    One function, called everywhere a week is derived. Two places computing
    "the start of the week" is two chances for one of them to use Sunday, and
    the symptom would be a plan that silently belongs to the wrong week.
    """
    return day - timedelta(days=day.weekday())


def to_engine(row: WeeklyPlanRow) -> WeeklyPlan:
    """One stored plan as the engine sees it.

    The engine's validation runs here, which is the point: a row that violates
    a rule -- a missed commitment with no reason, a plan not starting on a
    Monday -- raises rather than being quietly rendered.
    """
    commitments = []
    for stored in sorted(row.commitments, key=lambda c: (c.sequence, c.id)):
        commitments.append(
            Commitment(
                id=stored.id,
                activity_id=stored.activity_code,
                description=stored.description,
                crew=stored.crew,
                constraints=tuple(
                    Constraint(
                        id=c.id,
                        description=c.description,
                        kind=ConstraintKind(c.kind),
                        owner=c.owner,
                        promised_by=c.promised_by,
                        removed_on=c.removed_on,
                    )
                    for c in stored.constraints
                ),
                completed=stored.completed,
                reason=VarianceReason(stored.reason) if stored.reason else None,
                notes=stored.notes,
            )
        )
    return WeeklyPlan(week_starting=row.week_starting, commitments=tuple(commitments))


def reliability(project: Project) -> dict[str, Any] | None:
    """PPC over every stored week, or `None` when there are none.

    `None` rather than an empty report, because "this project does not use Last
    Planner" and "this project has planned nothing" are different states and
    the page says something different for each.
    """
    if not project.weekly_plans:
        return None
    weeks = [to_engine(row) for row in project.weekly_plans]
    return assess(weeks).to_dict()


def open_constraints(project: Project, *, on: date | None = None) -> list[dict[str, Any]]:
    """Every live constraint across every week, overdue first.

    The make-ready meeting's agenda. Sorted by promised date so the oldest
    broken promise is at the top, which is the one worth asking about.
    """
    on = on or date.today()
    rows: list[dict[str, Any]] = []
    for plan in project.weekly_plans:
        for stored in plan.commitments:
            for constraint in stored.constraints:
                live = constraint.removed_on is None or constraint.removed_on > on
                if not live:
                    continue
                rows.append(
                    {
                        "id": constraint.id,
                        "kind": constraint.kind,
                        "description": constraint.description,
                        "owner": constraint.owner,
                        "promised_by": constraint.promised_by.isoformat(),
                        "overdue_days": max(0, (on - constraint.promised_by).days),
                        "blocks": stored.description,
                        "week_starting": plan.week_starting.isoformat(),
                    }
                )
    rows.sort(key=lambda r: (-int(r["overdue_days"]), str(r["promised_by"]), str(r["id"])))
    return rows


def plan_for(project: Project, week_starting: date) -> WeeklyPlanRow | None:
    return next((p for p in project.weekly_plans if p.week_starting == week_starting), None)


def open_week(session: Session, project: Project, week_starting: date) -> WeeklyPlanRow:
    """Get or create the plan row for a week.

    Unique per project, enforced by the database as well as here: two plans for
    one week is two denominators for one PPC.
    """
    if week_starting.weekday() != 0:
        raise LastPlannerError(
            f"a weekly work plan starts on a Monday, not a {week_starting.strftime('%A')}"
        )
    existing = plan_for(project, week_starting)
    if existing is not None:
        return existing
    row = WeeklyPlanRow(
        organization_id=project.organization_id,
        project_id=project.id,
        week_starting=week_starting,
    )
    project.weekly_plans.append(row)
    session.flush()
    return row


def add_commitment(
    session: Session,
    plan: WeeklyPlanRow,
    *,
    description: str,
    crew: str,
    activity_code: str = "",
    constraints: list[dict[str, Any]] | None = None,
    allow_constrained: bool = False,
) -> CommitmentRow:
    """Add one promise to a week, refusing it if it is not make-ready.

    The refusal runs through `core.lastplanner.commit`, so the rule is the
    engine's and not a second copy of it here.

    **A refused commitment stores nothing, including its constraints.** That is
    deliberate and it is also a limitation worth naming: the constraint the
    caller described is used to explain the refusal and then discarded with the
    rest of the rejected commitment. Constraints reach the log by being
    recorded against work that *is* committed -- either already cleared, or
    through `allow_constrained` when importing history.

    A separate lookahead board, where constrained work lives before it is
    promised, is the missing half of Last Planner here. It is absent rather
    than half-built: storing rejected commitments in the same table as accepted
    ones would put them one boolean away from the PPC denominator, and that
    denominator is the thing this whole module protects.
    """
    if not description.strip():
        raise LastPlannerError("a commitment needs a description of the work")
    if not crew.strip():
        raise LastPlannerError("a commitment needs a crew to make it")

    row = CommitmentRow(
        organization_id=plan.organization_id,
        plan_id=plan.id,
        sequence=len(plan.commitments),
        activity_code=activity_code.strip()[:120],
        description=description.strip()[:400],
        crew=crew.strip()[:120],
    )
    for entry in constraints or []:
        row.constraints.append(
            ConstraintRow(
                organization_id=plan.organization_id,
                kind=ConstraintKind(entry["kind"]).value,
                description=str(entry["description"]).strip()[:400],
                owner=str(entry["owner"]).strip()[:120],
                promised_by=entry["promised_by"],
                removed_on=entry.get("removed_on"),
            )
        )
    plan.commitments.append(row)
    session.flush()

    # Validate through the engine, on the whole week, then let the caller's
    # transaction roll back if it refuses. Checking only the new commitment
    # would miss a plan that has become invalid for another reason.
    try:
        commit(
            plan.week_starting,
            list(to_engine(plan).commitments),
            on=plan.week_starting,
            allow_constrained=allow_constrained,
        )
    except LastPlannerError:
        plan.commitments.remove(row)
        session.flush()
        raise
    return row


def assess_commitment(
    session: Session,
    commitment: CommitmentRow,
    *,
    completed: bool,
    reason: str | None = None,
) -> CommitmentRow:
    """Record how a promise turned out.

    A missed commitment with no reason is refused by the engine's own
    validation, which is where that rule belongs -- the reasons are the whole
    learning loop, and a route that defaulted them would fill the log with
    "other".
    """
    parsed = VarianceReason(reason) if reason else None
    # Constructed purely for its validation. Cheap, and it means the rule has
    # exactly one home.
    Commitment(
        id=commitment.id,
        activity_id=commitment.activity_code,
        description=commitment.description,
        crew=commitment.crew,
        completed=completed,
        reason=None if completed else (parsed or VarianceReason.NOT_RECORDED),
    )
    commitment.completed = completed
    commitment.reason = None if completed else (parsed or VarianceReason.NOT_RECORDED).value
    session.flush()
    return commitment


def clear_constraint(
    session: Session, constraint: ConstraintRow, *, removed_on: date
) -> ConstraintRow:
    """Mark a constraint cleared, on a stated day.

    A date rather than a flag, because the engine reads a removal date in the
    future as still live today -- and "cleared next Friday" must not make this
    Monday's plan look ready.
    """
    constraint.removed_on = removed_on
    session.flush()
    return constraint
