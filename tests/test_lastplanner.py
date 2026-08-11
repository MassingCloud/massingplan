"""Last Planner, and mostly the ways PPC can be made to lie.

The arithmetic is one division. What the tests are about is everything the
division has to refuse: a denominator that shrinks after the fact, partial
credit for a broken promise, an unassessed week reported as a good one, and a
missed commitment with no reason attached.

Each of those is a way of making the number go up while the site gets worse,
which is the only interesting failure mode a metric has.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from massingplan.core.issues import Severity
from massingplan.core.lastplanner import (
    Commitment,
    Constraint,
    ConstraintKind,
    LastPlannerError,
    VarianceReason,
    WeeklyPlan,
    assess,
    commit,
    screen,
)

MONDAY = date(2026, 3, 2)  # a real Monday; asserted below rather than assumed


def _constraint(**overrides: object) -> Constraint:
    fields: dict = {
        "id": "C1",
        "description": "steel delivery",
        "kind": ConstraintKind.MATERIALS,
        "owner": "procurement",
        "promised_by": MONDAY - timedelta(days=7),
    }
    fields.update(overrides)
    return Constraint(**fields)  # type: ignore[arg-type]


def _commitment(**overrides: object) -> Commitment:
    fields: dict = {
        "id": "W1",
        "activity_id": "A1000",
        "description": "frame level 3",
        "crew": "Steel gang",
    }
    fields.update(overrides)
    return Commitment(**fields)  # type: ignore[arg-type]


def test_the_fixture_monday_really_is_a_monday() -> None:
    """The whole week model hangs off it, and a wrong constant here would make
    every span in this file quietly off by a day.
    """
    assert MONDAY.strftime("%A") == "Monday"


# -- the week ---------------------------------------------------------------


def test_a_week_starts_on_a_monday_and_is_half_open() -> None:
    """Same convention as every other span in the package: `week_ends` is the
    first day *not* in the week, and `last_day` is the Sunday.
    """
    plan = WeeklyPlan(week_starting=MONDAY, commitments=(_commitment(),))
    assert plan.last_day == date(2026, 3, 8)
    assert plan.last_day.strftime("%A") == "Sunday"
    assert plan.week_ends == date(2026, 3, 9)
    assert plan.week_ends.strftime("%A") == "Monday"


def test_a_plan_that_does_not_start_on_a_monday_is_refused() -> None:
    with pytest.raises(LastPlannerError, match="Monday"):
        WeeklyPlan(week_starting=date(2026, 3, 3), commitments=())


def test_duplicate_commitment_ids_are_refused() -> None:
    with pytest.raises(LastPlannerError, match="duplicate"):
        WeeklyPlan(week_starting=MONDAY, commitments=(_commitment(), _commitment()))


# -- PPC, and what it refuses ----------------------------------------------


def test_ppc_is_completed_over_committed() -> None:
    plan = WeeklyPlan(
        week_starting=MONDAY,
        commitments=(
            _commitment(id="W1", completed=True),
            _commitment(id="W2", completed=True),
            _commitment(id="W3", completed=True),
            _commitment(id="W4", completed=False, reason=VarianceReason.MATERIALS),
        ),
    )
    assert plan.ppc() == 0.75
    assert (plan.committed, plan.completed) == (4, 3)


def test_an_unassessed_commitment_makes_the_week_unmeasurable_not_perfect() -> None:
    """The tri-state, and the reason for it.

    Three of four met and one never answered is *not* 100%, and it is not 75%
    either -- the answer moves once somebody fills it in, and a number that
    will change is not a measurement. Same discipline as a skipped DCMA check.
    """
    plan = WeeklyPlan(
        week_starting=MONDAY,
        commitments=(
            _commitment(id="W1", completed=True),
            _commitment(id="W2", completed=True),
            _commitment(id="W3", completed=True),
            _commitment(id="W4"),  # never assessed
        ),
    )
    assert plan.ppc() is None
    assert len(plan.unassessed) == 1
    assert plan.completed == 3


def test_a_week_nobody_planned_is_unmeasurable_rather_than_zero() -> None:
    """0% would blame a crew for a week that was never planned, which makes the
    metric a punishment rather than a measurement and guarantees it gets gamed.
    """
    assert WeeklyPlan(week_starting=MONDAY, commitments=()).ppc() is None


def test_the_denominator_is_every_commitment_made() -> None:
    """The commonest way PPC is gamed: drop the ones that went badly before
    reporting. The plan is frozen, so the only way to remove a commitment is to
    build a different plan -- which is visibly a different plan.
    """
    made = [
        _commitment(id="W1", completed=True),
        _commitment(id="W2", completed=False, reason=VarianceReason.LABOUR),
        _commitment(id="W3", completed=False, reason=VarianceReason.WEATHER),
    ]
    honest = WeeklyPlan(week_starting=MONDAY, commitments=tuple(made))
    assert honest.committed == 3
    assert honest.ppc() == pytest.approx(1 / 3)

    # A tuple, so the stored plan cannot be edited in place after the fact.
    with pytest.raises((AttributeError, TypeError)):
        honest.commitments.append(_commitment(id="W4"))  # type: ignore[attr-defined]


def test_there_is_no_partial_credit() -> None:
    """ "Nearly done" is a commitment that was not met. Averaging in a fraction
    is how a week of broken promises reports as 90%.
    """
    plan = WeeklyPlan(
        week_starting=MONDAY,
        commitments=(
            _commitment(id="W1", completed=False, reason=VarianceReason.LABOUR, notes="95% done"),
        ),
    )
    assert plan.ppc() == 0.0


def test_finishing_more_than_promised_is_still_one_hundred_percent() -> None:
    """PPC measures the reliability of the promise, not the volume of work. A
    crew that doubled its output promised badly, and the number says so.
    """
    plan = WeeklyPlan(
        week_starting=MONDAY,
        commitments=(_commitment(id="W1", completed=True), _commitment(id="W2", completed=True)),
    )
    assert plan.ppc() == 1.0


# -- the reason is not optional --------------------------------------------


def test_a_missed_commitment_without_a_reason_is_refused() -> None:
    """The one piece of data the whole method exists to collect."""
    with pytest.raises(LastPlannerError, match="needs a reason"):
        _commitment(completed=False)


def test_not_recorded_is_a_reason_so_that_it_can_be_counted() -> None:
    """A blank field and "nobody said why" are the same fact. Naming it makes
    the count of unexplained failures reportable, which is usually the first
    thing worth fixing.
    """
    plan = WeeklyPlan(
        week_starting=MONDAY,
        commitments=(_commitment(id="W1", completed=False, reason=VarianceReason.NOT_RECORDED),),
    )
    assert plan.variance() == {VarianceReason.NOT_RECORDED: 1}


def test_a_completed_commitment_cannot_also_carry_a_reason() -> None:
    with pytest.raises(LastPlannerError, match="no variance reason"):
        _commitment(completed=True, reason=VarianceReason.WEATHER)


def test_a_commitment_needs_a_crew_to_make_it() -> None:
    """A promise nobody made is not a promise."""
    with pytest.raises(LastPlannerError, match="crew"):
        _commitment(crew="")


# -- constraints and make-ready --------------------------------------------


def test_a_constraint_removed_later_is_still_live_today() -> None:
    """Reading `removed_on is None` alone would let a constraint cleared next
    Friday make this Monday's plan look ready -- which is exactly the plan that
    then fails.
    """
    cleared_friday = _constraint(removed_on=MONDAY + timedelta(days=4))
    assert cleared_friday.is_live(MONDAY)
    assert not cleared_friday.is_live(MONDAY + timedelta(days=4))
    assert not cleared_friday.is_live(MONDAY + timedelta(days=5))


def test_a_constraint_is_overdue_only_while_it_is_live() -> None:
    late = _constraint(promised_by=MONDAY - timedelta(days=7))
    assert late.is_overdue(MONDAY)
    assert not _constraint(
        promised_by=MONDAY - timedelta(days=7), removed_on=MONDAY - timedelta(days=1)
    ).is_overdue(MONDAY)


def test_a_constraint_needs_an_owner() -> None:
    """One with no owner is not being removed by anybody."""
    with pytest.raises(LastPlannerError, match="owner"):
        _constraint(owner="")


def test_make_ready_means_every_constraint_cleared() -> None:
    blocked = _commitment(constraints=(_constraint(), _constraint(id="C2")))
    assert not blocked.is_make_ready(MONDAY)
    assert len(blocked.live_constraints(MONDAY)) == 2

    half = _commitment(
        constraints=(_constraint(removed_on=MONDAY - timedelta(days=1)), _constraint(id="C2"))
    )
    assert not half.is_make_ready(MONDAY), "one live constraint still blocks"

    ready = _commitment(constraints=(_constraint(removed_on=MONDAY - timedelta(days=1)),))
    assert ready.is_make_ready(MONDAY)


def test_screening_splits_the_lookahead_and_names_the_overdue(caplog) -> None:  # type: ignore[no-untyped-def]
    ready, blocked = screen(
        [
            _commitment(id="W1"),
            _commitment(id="W2", constraints=(_constraint(id="C2"),)),
        ],
        on=MONDAY,
    )
    assert [c.id for c in ready] == ["W1"]
    assert [c.id for c in blocked] == ["W2"]
    del caplog


def test_screening_reports_an_overdue_constraint_with_its_owner() -> None:
    """The make-ready meeting's agenda: what is late, who owns it, what it
    blocks. "Three constraints outstanding" is not actionable.
    """
    from massingplan.core.issues import IssueLog

    issues = IssueLog()
    screen(
        [_commitment(constraints=(_constraint(promised_by=MONDAY - timedelta(days=14)),))],
        on=MONDAY,
        issues=issues,
    )
    assert issues.has("LP_CONSTRAINT_OVERDUE")
    entry = issues.by_severity(Severity.WARNING)[0]
    assert "steel delivery" in entry.message
    assert "procurement" in entry.action
    assert "frame level 3" in entry.action


# -- committing -------------------------------------------------------------


def test_committing_constrained_work_is_refused_not_warned_about() -> None:
    """The method, in one assertion.

    A system that merely warns will be used to commit constrained work every
    single week, because by the third week the warning is wallpaper.
    """
    with pytest.raises(LastPlannerError, match="not make-ready"):
        commit(MONDAY, [_commitment(constraints=(_constraint(),))])


def test_the_refusal_names_what_is_blocking_and_how_many() -> None:
    with pytest.raises(LastPlannerError) as caught:
        commit(
            MONDAY,
            [_commitment(id=f"W{n}", constraints=(_constraint(id=f"C{n}"),)) for n in range(8)],
        )
    message = str(caught.value)
    assert "8 of 8" in message
    assert "materials" in message
    assert "and 3 more" in message, "a capped list must say it was capped"


def test_make_ready_work_commits_cleanly() -> None:
    plan = commit(
        MONDAY,
        [_commitment(constraints=(_constraint(removed_on=MONDAY - timedelta(days=3)),))],
    )
    assert plan.week_starting == MONDAY
    assert plan.committed == 1


def test_history_can_be_imported_with_constraints_that_were_never_recorded() -> None:
    """An escape hatch for data, not a convenience for planning: refusing here
    would mean losing the history rather than improving the plan.
    """
    plan = commit(MONDAY, [_commitment(constraints=(_constraint(),))], allow_constrained=True)
    assert plan.committed == 1


# -- reliability over time --------------------------------------------------


def _week(offset_weeks: int, met: int, missed: int, reason: VarianceReason) -> WeeklyPlan:
    start = MONDAY + timedelta(weeks=offset_weeks)
    commitments = [_commitment(id=f"{offset_weeks}-ok-{n}", completed=True) for n in range(met)] + [
        _commitment(id=f"{offset_weeks}-no-{n}", completed=False, reason=reason)
        for n in range(missed)
    ]
    return WeeklyPlan(week_starting=start, commitments=tuple(commitments))


def test_the_trend_is_a_series_not_an_average() -> None:
    """Five good weeks and one collapse is a project with a problem in week
    six. The average is 72% and shows nothing at all.
    """
    weeks = [_week(n, 8, 2, VarianceReason.MATERIALS) for n in range(5)]
    weeks.append(_week(5, 3, 7, VarianceReason.LABOUR))
    report = assess(weeks)

    trend = report.trend()
    assert [round(v or 0, 2) for _d, v in trend] == [0.8, 0.8, 0.8, 0.8, 0.8, 0.3]
    assert [d for d, _v in trend] == sorted(d for d, _v in trend), "in week order"


def test_the_mean_is_taken_over_measurable_weeks_only() -> None:
    """Averaging an unmeasurable week as zero punishes a project for a
    reporting failure as though it were a production one.
    """
    weeks = [
        _week(0, 8, 2, VarianceReason.MATERIALS),
        WeeklyPlan(week_starting=MONDAY + timedelta(weeks=1), commitments=(_commitment(),)),
    ]
    report = assess(weeks)
    assert report.trend()[1][1] is None
    assert report.mean_ppc() == pytest.approx(0.8)
    assert len(report.measurable_weeks()) == 1


def test_the_mean_is_none_when_nothing_is_measurable() -> None:
    report = assess([WeeklyPlan(week_starting=MONDAY, commitments=(_commitment(),))])
    assert report.mean_ppc() is None


def test_the_top_reasons_are_stable_and_ordered_by_count() -> None:
    """A tiebreak on the name, so the same data gives the same answer between
    runs rather than depending on dict ordering.
    """
    weeks = [
        _week(0, 0, 5, VarianceReason.MATERIALS),
        _week(1, 0, 3, VarianceReason.LABOUR),
        _week(2, 0, 3, VarianceReason.DESIGN_CHANGE),
    ]
    report = assess(weeks)
    assert report.top_reasons() == [
        (VarianceReason.MATERIALS, 5),
        (VarianceReason.DESIGN_CHANGE, 3),
        (VarianceReason.LABOUR, 3),
    ]


def test_an_unassessed_week_is_reported_as_a_measurement_problem() -> None:
    weeks = [
        WeeklyPlan(
            week_starting=MONDAY,
            commitments=(_commitment(id="W1", completed=True), _commitment(id="W2")),
        )
    ]
    report = assess(weeks)
    assert report.issues.has("LP_WEEK_NOT_ASSESSED")
    assert report.trend()[0][1] is None


def test_an_empty_week_says_so_rather_than_scoring_zero() -> None:
    report = assess([WeeklyPlan(week_starting=MONDAY, commitments=())])
    assert report.issues.has("LP_EMPTY_WEEK")
    assert report.mean_ppc() is None


def test_unexplained_failures_are_counted_and_reported() -> None:
    report = assess([_week(0, 5, 5, VarianceReason.NOT_RECORDED)])
    assert report.issues.has("LP_VARIANCE_UNEXPLAINED")
    assert report.variance()[VarianceReason.NOT_RECORDED] == 5


def test_the_report_serialises_to_the_shape_a_page_can_render() -> None:
    report = assess(
        [_week(0, 8, 2, VarianceReason.MATERIALS), _week(1, 6, 4, VarianceReason.LABOUR)]
    )
    payload = report.to_dict()

    assert [w["ppc"] for w in payload["weeks"]] == [0.8, 0.6]  # type: ignore[index]
    assert payload["measurable_weeks"] == 2
    assert payload["top_reasons"][0] == {"reason": "labour", "count": 4}  # type: ignore[index]
    weeks = payload["weeks"]
    assert weeks[0]["last_day"] == "2026-03-08"  # type: ignore[index]

    import json

    json.dumps(payload)  # the page gets it as JSON; unserialisable is a 500
