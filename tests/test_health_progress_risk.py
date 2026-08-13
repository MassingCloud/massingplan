"""DCMA assessment, progress measurement and Monte Carlo risk.

The through-line: a measurement that could not be made must say so, rather than
returning a number that reads as a measurement.
"""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.constraints import ConstraintType
from massingplan.core.health import HIGH_FLOAT_DAYS, PROBE_DAYS, assess
from massingplan.core.network import ActivityKind, Link, RelationType, SchedulerOptions, Task
from massingplan.core.progress import build_report
from massingplan.core.risk import (
    Distribution,
    DurationEstimate,
    default_estimates,
    simulate,
)
from massingplan.core.schedule import schedule_network

JUN1 = date(2026, 6, 1)


def chain(n: int = 5, duration: int = 4) -> tuple[list[Task], list[Link]]:
    """A clean n-activity FS chain. Should score well on every runnable check."""
    tasks = [Task(f"A{i}", f"Activity {i}", duration, "5D") for i in range(n)]
    links = [Link(f"A{i}", f"A{i + 1}") for i in range(n - 1)]
    return tasks, links


def run(tasks, links, five_day, **kw):  # type: ignore[no-untyped-def]
    return schedule_network(tasks, links, {"5D": five_day}, data_date=JUN1, **kw)


# -- DCMA: the honesty rule ------------------------------------------------


def test_a_clean_chain_scores_well_and_is_optimisable(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links = chain()
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    assert report.grade in ("A", "B")
    assert report.is_optimisable
    assert report.check(1).passed is True


def test_checks_without_their_data_are_skipped_not_passed(five_day) -> None:  # type: ignore[no-untyped-def]
    """The rule that makes the score mean anything.

    Four checks need a baseline, actuals or resources. Reporting them as passes
    would score a schedule 14/14 for data it never had.
    """
    tasks, links = chain()
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    skipped = {c.number for c in report.skipped}
    assert skipped == {9, 10, 11, 14}
    for number in skipped:
        assert report.check(number).status == "skipped"
        assert "Skipped:" in report.check(number).detail


def test_skipped_checks_are_excluded_from_the_denominator(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links = chain()
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    assert len(report.assessed) == 10
    expected = 100.0 * sum(1 for c in report.assessed if c.passed) / 10
    assert report.score == pytest.approx(expected)


def test_supplying_resources_turns_check_ten_on(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links = chain()
    report = assess(
        run(tasks, links, five_day),
        tasks,
        links,
        {"5D": five_day},
        resourced_activity_ids=[t.id for t in tasks],
    )
    assert report.check(10).passed is True
    assert len(report.assessed) == 11


# -- DCMA: individual checks -----------------------------------------------


def test_check_one_exempts_one_start_and_one_finish_by_network_position(five_day) -> None:  # type: ignore[no-untyped-def]
    """Exactly one open start and one open finish are legitimate.

    Chosen by where they sit in the network, not by where they sit in the input
    list -- list position says nothing about the plan.
    """
    tasks, links = chain(4)
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    assert report.check(1).offenders == []

    detached = [*tasks, Task("ORPHAN", "Detached", 3, "5D")]
    report2 = assess(run(detached, links, five_day), detached, links, {"5D": five_day})
    assert "ORPHAN" in report2.check(1).offenders


def test_check_two_flags_leads_and_check_three_flags_lags(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, _ = chain(3)
    links = [Link("A0", "A1", RelationType.FS, -2), Link("A1", "A2", RelationType.FS, 5)]
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    assert report.check(2).passed is False
    assert report.check(2).offenders == ["A0->A1"]
    assert report.check(3).passed is False
    assert report.check(3).offenders == ["A1->A2"]


def test_check_four_wants_ninety_percent_finish_to_start(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, _ = chain(3)
    links = [Link("A0", "A1", RelationType.SS), Link("A1", "A2", RelationType.FS)]
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    assert report.check(4).passed is False
    assert "SS" in report.check(4).offenders[0]


def test_check_five_now_sees_all_ten_constraint_types(five_day) -> None:  # type: ignore[no-untyped-def]
    """The surgery. The source counted two of ten; eight were invisible to the
    check that exists to find them.
    """
    tasks, links = chain(4)
    tasks[1] = Task(
        "A1",
        "Pinned",
        4,
        "5D",
        constraint=ConstraintType.MANDATORY_FINISH,
        constraint_date=date(2026, 6, 30),
    )
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    assert "A1" in report.check(5).offenders


def test_check_five_counts_a_soft_constraint_separately(five_day) -> None:  # type: ignore[no-untyped-def]
    """SNET records a delivery date. That is a fact, not an override."""
    tasks, links = chain(4)
    tasks[1] = Task(
        "A1",
        "Awaiting delivery",
        4,
        "5D",
        constraint=ConstraintType.START_ON_OR_AFTER,
        constraint_date=date(2026, 6, 30),
    )
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    assert report.check(5).offenders == []
    assert "1 more carry a soft one" in report.check(5).detail


def test_check_six_measures_float_in_working_days_not_calendar_days(five_day) -> None:  # type: ignore[no-untyped-def]
    """Five days of slack across a shutdown must not report as fourteen."""
    tasks = [
        Task("LONG", "", 60, "5D"),
        Task("SHORT", "", 1, "5D"),
        Task("END", "", 1, "5D"),
    ]
    links = [Link("LONG", "END"), Link("SHORT", "END")]
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    assert "SHORT" in report.check(6).offenders
    assert report.check(6).passed is False
    # And the value is the working-day count, well above the 44-day threshold.
    outcome = run(tasks, links, five_day)
    assert outcome.dates["SHORT"].total_float_days > HIGH_FLOAT_DAYS


def test_check_seven_counts_negative_float_and_skips_completed_work(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks = [
        Task("DONE", "", 5, "5D", actual_start=date(2026, 5, 1), actual_finish=date(2026, 5, 8)),
        Task("LATE", "", 20, "5D"),
    ]
    links = [Link("DONE", "LATE")]
    outcome = run(tasks, links, five_day, options=SchedulerOptions(must_finish_by=date(2026, 6, 5)))
    report = assess(outcome, tasks, links, {"5D": five_day})
    assert report.check(7).offenders == ["LATE"]
    assert "DONE" not in report.check(7).offenders


def test_check_twelve_reduces_to_the_classic_assertion_on_one_calendar(five_day) -> None:  # type: ignore[no-untyped-def]
    """Under a single calendar the multi-calendar restatement must be exactly
    "the finish moved by the injected delay". Otherwise the restatement has
    quietly weakened the check.
    """
    tasks, links = chain(4)
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    check = report.check(12)
    assert check.passed is True
    assert check.value == float(PROBE_DAYS)
    assert f"{PROBE_DAYS} working days" in check.detail


def test_check_twelve_fails_when_a_constraint_absorbs_the_delay(five_day) -> None:  # type: ignore[no-untyped-def]
    """A mandatory finish downstream swallows the probe, so the reported
    critical path is not the one that governs.
    """
    tasks = [
        Task("A", "", 10, "5D"),
        Task(
            "B",
            "",
            5,
            "5D",
            constraint=ConstraintType.MANDATORY_FINISH,
            constraint_date=date(2026, 7, 31),
        ),
    ]
    links = [Link("A", "B")]
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    assert report.check(12).passed is False


def test_check_twelve_probes_an_incomplete_activity(five_day) -> None:  # type: ignore[no-untyped-def]
    """Delaying finished work proves nothing; its dates are history."""
    tasks = [
        Task("DONE", "", 5, "5D", actual_start=date(2026, 5, 1), actual_finish=date(2026, 5, 8)),
        Task("NEXT", "", 5, "5D"),
    ]
    links = [Link("DONE", "NEXT")]
    report = assess(run(tasks, links, five_day), tasks, links, {"5D": five_day})
    assert "DONE" not in report.check(12).detail


def test_check_fourteen_skips_when_nothing_was_due_rather_than_scoring_one(
    five_day,
) -> None:  # type: ignore[no-untyped-def]
    """An empty ratio is no information, not perfect performance."""
    tasks, links = chain(3)
    baseline = {t.id: date(2027, 1, 1) for t in tasks}  # all in the future
    report = assess(
        run(tasks, links, five_day),
        tasks,
        links,
        {"5D": five_day},
        baseline_finish=baseline,
    )
    assert report.check(14).passed is None
    assert "no baselined activity was due" in report.check(14).detail


def test_the_report_is_json_safe(five_day) -> None:  # type: ignore[no-untyped-def]
    import json

    tasks, links = chain()
    json.dumps(assess(run(tasks, links, five_day), tasks, links, {"5D": five_day}).to_dict())


def test_is_optimisable_gates_on_logic_negative_float_and_cpli(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks = [Task("A", "", 20, "5D")]
    outcome = run(tasks, [], five_day, options=SchedulerOptions(must_finish_by=date(2026, 6, 5)))
    report = assess(outcome, tasks, [], {"5D": five_day})
    assert report.check(7).passed is False
    assert report.is_optimisable is False


# -- progress --------------------------------------------------------------


def test_baseline_execution_index_is_none_when_nothing_was_due() -> None:
    """Not 1.0. A green tile for a project that has not started is a lie."""
    tasks = [Task("A", "", 5, "5D")]
    report = build_report(tasks, date(2026, 6, 1), baseline_finish={"A": date(2026, 12, 1)})
    assert report.baseline_execution_index is None
    assert report.to_dict()["baseline_execution_index"] is None
    assert report.to_dict()["baseline_execution_index_reason"]


def test_baseline_execution_index_counts_what_finished_against_what_was_due(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks = [
        Task("A", "", 5, "5D", actual_start=date(2026, 6, 1), actual_finish=date(2026, 6, 5)),
        Task("B", "", 5, "5D"),
    ]
    report = build_report(
        tasks,
        date(2026, 6, 20),
        baseline_finish={"A": date(2026, 6, 5), "B": date(2026, 6, 12)},
        calendars={"5D": five_day},
    )
    assert report.baseline_execution_index == pytest.approx(0.5)


def test_is_behind_catches_due_and_never_started_which_dcma_eleven_misses() -> None:
    """The broadening. Check 11 only looks at finishes; this is more urgent."""
    tasks = [Task("A", "", 5, "5D")]
    report = build_report(
        tasks,
        date(2026, 6, 20),
        baseline_start={"A": date(2026, 6, 1)},
        baseline_finish={"A": date(2026, 6, 5)},
    )
    assert report.not_started_but_due == ["A"]
    assert len(report.behind) == 1


def test_variance_is_measured_in_working_days_not_calendar_days(five_day) -> None:  # type: ignore[no-untyped-def]
    """Baseline Friday, actual the following Wednesday: three working days, not five."""
    tasks = [Task("A", "", 5, "5D", actual_start=date(2026, 6, 1), actual_finish=date(2026, 6, 10))]
    report = build_report(
        tasks,
        date(2026, 6, 20),
        baseline_finish={"A": date(2026, 6, 5)},
        calendars={"5D": five_day},
    )
    assert report.activities[0].finish_variance_days == 3


def test_an_actual_finish_without_a_start_is_reported_as_invalid() -> None:
    from dataclasses import dataclass

    # Task validates this at construction, so build the invalid shape directly.
    @dataclass(frozen=True)
    class Loose:
        id: str = "A"
        name: str = ""
        duration_days: int = 5
        calendar_id: str = "5D"
        actual_start: date | None = None
        actual_finish: date | None = date(2026, 6, 5)
        remaining_days: int | None = None

    report = build_report([Loose()], date(2026, 6, 20))  # type: ignore[list-item]
    assert report.invalid_actuals == ["A"]


# -- risk ------------------------------------------------------------------


def test_the_same_seed_gives_the_same_forecast(five_day) -> None:  # type: ignore[no-untyped-def]
    """A forecast that changes when nobody changed the plan is not actionable."""
    tasks, links = chain(4)
    a = simulate(tasks, links, {"5D": five_day}, iterations=200, data_date=JUN1)
    b = simulate(tasks, links, {"5D": five_day}, iterations=200, data_date=JUN1)
    assert a.finishes == b.finishes
    assert a.to_dict() == b.to_dict()


def test_percentiles_are_ordered_and_round_up(five_day) -> None:  # type: ignore[no-untyped-def]
    """A P80 of 271.2 days is not met on day 271."""
    tasks, links = chain(5)
    result = simulate(tasks, links, {"5D": five_day}, iterations=400, data_date=JUN1)
    p10, p50, p80, p90 = (result.percentile(p) for p in (10, 50, 80, 90))
    assert p10 <= p50 <= p80 <= p90


def test_the_deterministic_date_is_usually_optimistic_and_we_measure_it(five_day) -> None:  # type: ignore[no-untyped-def]
    """The most useful single number a risk run produces."""
    tasks, links = chain(6)
    result = simulate(tasks, links, {"5D": five_day}, iterations=400, data_date=JUN1)
    confidence = result.confidence_in_deterministic
    assert confidence is not None
    assert 0.0 <= confidence <= 1.0
    # A right-skewed default spread should not clear the deterministic date more
    # than half the time on a six-activity chain.
    assert confidence < 0.5


def test_a_sole_path_activity_has_a_criticality_index_of_one(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links = chain(3)
    result = simulate(tasks, links, {"5D": five_day}, iterations=100, data_date=JUN1)
    for risk in result.activities:
        assert risk.criticality_index == 1.0


def test_a_zero_spread_estimate_produces_a_single_outcome(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links = chain(3)
    estimates = [DurationEstimate(t.id, 4, 4, 4) for t in tasks]
    result = simulate(
        tasks, links, {"5D": five_day}, estimates=estimates, iterations=50, data_date=JUN1
    )
    assert len(set(result.finishes)) == 1


def test_estimates_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="must be ordered"):
        DurationEstimate("A", optimistic=10, most_likely=5, pessimistic=8)


def test_default_estimates_are_right_skewed() -> None:
    est = default_estimates([Task("A", "", 10, "5D")])[0]
    assert est.most_likely - est.optimistic < est.pessimistic - est.most_likely


def test_a_missing_estimate_is_named_rather_than_defaulted(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links = chain(3)
    with pytest.raises(ValueError, match="No duration estimate for: A2"):
        simulate(
            tasks,
            links,
            {"5D": five_day},
            estimates=[DurationEstimate(t.id, 3, 4, 5) for t in tasks[:2]],
            iterations=10,
            data_date=JUN1,
        )


def test_both_distributions_run(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links = chain(3)
    for dist in Distribution:
        result = simulate(
            tasks, links, {"5D": five_day}, iterations=50, distribution=dist, data_date=JUN1
        )
        assert result.distribution is dist
        assert len(result.finishes) == 50


def test_the_risk_result_is_json_safe(five_day) -> None:  # type: ignore[no-untyped-def]
    import json

    tasks, links = chain(3)
    json.dumps(simulate(tasks, links, {"5D": five_day}, iterations=50, data_date=JUN1).to_dict())


# -- the forecast and the schedule name the same day -------------------------


def _flat(tasks):  # type: ignore[no-untyped-def]
    """Estimates with no spread, so every iteration reproduces the CPM run."""
    return [
        DurationEstimate(
            t.id, float(t.duration_days), float(t.duration_days), float(t.duration_days)
        )
        for t in tasks
    ]


def test_the_forecast_reports_the_same_finish_the_schedule_does(five_day) -> None:  # type: ignore[no-untyped-def]
    """With no spread every iteration is the deterministic run, so every
    percentile must land on the date the activity table shows.

    It did not. `simulate` converted the finish with a bare `day_of`, which
    names the day *after* the last one worked -- so the whole forecast, P10
    through P90, sat one day later than the project finish printed beside it.
    A P80 is read off against a contract date.
    """
    tasks = [Task("A", "Walls", 5, "5D"), Task("B", "Roof", 3, "5D")]
    links = [Link("A", "B", RelationType.FS, 0)]
    outcome = schedule_network(tasks, links, {"5D": five_day}, data_date=date(2026, 6, 1))
    result = simulate(
        tasks,
        links,
        {"5D": five_day},
        estimates=_flat(tasks),
        iterations=25,
        data_date=date(2026, 6, 1),
    )
    assert result.deterministic_finish == outcome.project_finish
    for p in (0, 10, 50, 80, 90, 100):
        assert result.percentile(p) == outcome.project_finish, f"P{p}"


def test_a_forecast_never_lands_on_a_day_nobody_works(five_day) -> None:  # type: ignore[no-untyped-def]
    """The half-open boundary is frequently a Saturday.

    Converting it directly reported project finishes on the weekend -- a
    tell that the number had not been through `_present` at all.
    """
    tasks = [Task("A", "Walls", 5, "5D")]  # Mon-Fri, boundary falls on the Saturday
    result = simulate(
        tasks,
        [],
        {"5D": five_day},
        estimates=_flat(tasks),
        iterations=10,
        data_date=date(2026, 6, 1),
    )
    assert result.deterministic_finish is not None
    assert result.deterministic_finish.weekday() < 5, result.deterministic_finish
    assert all(f.weekday() < 5 for f in result.finishes)


def test_a_completion_milestone_is_forecast_at_its_own_date(five_day) -> None:  # type: ignore[no-untyped-def]
    """The case every construction programme ends in, and the worst of the set.

    A finish milestone's start and finish are one instant, so the bare
    conversion was out by three days here, not one.
    """
    tasks = [
        Task("A", "Walls", 5, "5D"),
        Task("PC", "Practical Completion", 0, "5D", kind=ActivityKind.FINISH_MILESTONE),
    ]
    links = [Link("A", "PC", RelationType.FS, 0)]
    outcome = schedule_network(tasks, links, {"5D": five_day}, data_date=date(2026, 6, 1))
    result = simulate(
        tasks,
        links,
        {"5D": five_day},
        estimates=_flat(tasks),
        iterations=10,
        data_date=date(2026, 6, 1),
    )
    assert result.deterministic_finish == outcome.project_finish == date(2026, 6, 5)


def test_the_percentiles_never_go_backwards(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks = [Task(f"A{i}", f"Act {i}", 2 + i, "5D") for i in range(6)]
    links = [Link(f"A{i - 1}", f"A{i}", RelationType.FS, 0) for i in range(1, 6)]
    result = simulate(tasks, links, {"5D": five_day}, iterations=400, data_date=date(2026, 6, 1))
    dates = [result.percentile(p) for p in (0, 10, 25, 50, 75, 80, 90, 100)]
    assert dates == sorted(d for d in dates if d is not None)
    assert all(d is not None and d.weekday() < 5 for d in dates)


# -- the optional argument that was not optional ----------------------------


def test_the_report_runs_when_calendars_are_left_to_the_default(five_day) -> None:  # type: ignore[no-untyped-def]
    """`calendars` is documented optional. Taking that at face value cost the
    caller the entire report, not one check.

    Check 12 ended at `next(iter(calendars.values()))` on an empty mapping and
    a bare StopIteration came out of `assess`. Nothing in this repo hit it --
    both callers pass calendars -- but `core/` is copied into another codebase,
    where the first caller to use the documented signature would have.
    """
    tasks = [Task("A", "Walls", 5, "5D"), Task("B", "Roof", 3, "5D")]
    links = [Link("A", "B", RelationType.FS, 0)]
    outcome = schedule_network(tasks, links, {"5D": five_day}, data_date=date(2026, 6, 1))

    report = assess(outcome, tasks, links)  # no calendars
    assert len(report.checks) == 14

    explicit = assess(outcome, tasks, links, {"5D": five_day})
    for omitted, supplied in zip(report.checks, explicit.checks, strict=True):
        assert (omitted.number, omitted.passed, omitted.value) == (
            supplied.number,
            supplied.passed,
            supplied.value,
        ), f"check {omitted.number} differs when calendars are defaulted"
