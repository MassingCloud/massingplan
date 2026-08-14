"""Earned Schedule, worked by hand.

The baseline every test reads from, on a Mon-Fri calendar:

    A  Mon 1 Jun - Fri 5 Jun      5 working days
    B  Mon 8 Jun - Fri 12 Jun     5 working days
                                 10 working days planned

Data dates are Mondays, because the data date is the first day of *remaining*
work: a project whose last day worked was Friday has a Monday data date.
"""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.earned import EarnedScheduleError, measure
from massingplan.core.network import Task

BASELINE = {
    "A": (date(2026, 6, 1), date(2026, 6, 5)),
    "B": (date(2026, 6, 8), date(2026, 6, 12)),
}


def done(activity_id: str, started: date, finished: date) -> Task:
    return Task(
        activity_id,
        activity_id,
        5,
        "5D",
        actual_start=started,
        actual_finish=finished,
        remaining_days=0,
    )


def todo(activity_id: str) -> Task:
    return Task(activity_id, activity_id, 5, "5D")


# -- the reason this module exists ------------------------------------------


def test_the_index_stays_honest_at_completion(five_day) -> None:  # type: ignore[no-untyped-def]
    """The defect in SPI, and the fix, in one assertion.

    Both activities took ten working days instead of five, so the job finished
    Fri 26 June against a baseline of Fri 12 June -- a fortnight late, and
    complete. Classic `EV / PV` reads **1.0**: every activity is earned in full,
    so earned equals planned and the ratio is 1. A dashboard shows green on a
    project that ran double its duration.

    Earned Schedule measures on the time axis instead. Ten days of baseline
    were earned over twenty elapsed, so SPI(t) is 0.5 and keeps saying so.
    """
    tasks = [
        done("A", date(2026, 6, 1), date(2026, 6, 12)),
        done("B", date(2026, 6, 15), date(2026, 6, 26)),
    ]
    result = measure(tasks, BASELINE, data_date=date(2026, 6, 29), calendar=five_day)

    assert result.percent_complete == 1.0
    assert result.classic_performance_index == pytest.approx(1.0), "the metric being replaced"

    assert result.actual_time_days == 20
    assert result.earned_days == 10.0
    assert result.performance_index == pytest.approx(0.5)
    assert result.schedule_variance_days == pytest.approx(-10.0)
    assert result.to_dict()["is_behind"] is True


# -- the ordinary cases, by hand -------------------------------------------


def test_a_project_exactly_on_plan_reads_one(five_day) -> None:  # type: ignore[no-untyped-def]
    """A done on time, B not started, one week gone: five earned in five."""
    result = measure(
        [done("A", date(2026, 6, 1), date(2026, 6, 5)), todo("B")],
        BASELINE,
        data_date=date(2026, 6, 8),
        calendar=five_day,
    )
    assert result.actual_time_days == 5
    assert result.earned_days == 5.0
    assert result.performance_index == pytest.approx(1.0)
    assert result.schedule_variance_days == pytest.approx(0.0)


def test_a_week_gone_with_nothing_done_reads_zero(five_day) -> None:  # type: ignore[no-untyped-def]
    """Not `None`, and not 1.0. No progress is a measurement, not an absence."""
    result = measure(
        [todo("A"), todo("B")], BASELINE, data_date=date(2026, 6, 8), calendar=five_day
    )
    assert result.actual_time_days == 5
    assert result.earned_days == 0.0
    assert result.performance_index == pytest.approx(0.0)
    assert result.schedule_variance_days == pytest.approx(-5.0)


def test_partial_progress_is_earned_linearly_within_an_activity(five_day) -> None:  # type: ignore[no-untyped-def]
    """A five-day activity reporting 60% has earned three of its days."""
    partial = Task("A", "A", 5, "5D", actual_start=date(2026, 6, 1), percent_complete=60.0)
    result = measure([partial, todo("B")], BASELINE, data_date=date(2026, 6, 8), calendar=five_day)
    assert result.earned_duration_days == pytest.approx(3.0)
    assert result.earned_days == pytest.approx(3.0)


def test_remaining_duration_is_used_when_no_percentage_was_given(five_day) -> None:  # type: ignore[no-untyped-def]
    """Two of five days left is three earned, without anybody claiming a number."""
    partial = Task("A", "A", 5, "5D", actual_start=date(2026, 6, 1), remaining_days=2)
    result = measure([partial, todo("B")], BASELINE, data_date=date(2026, 6, 8), calendar=five_day)
    assert result.earned_duration_days == pytest.approx(3.0)


def test_an_actual_finish_outranks_a_stale_percentage(five_day) -> None:  # type: ignore[no-untyped-def]
    """An actual finish is a fact; a percentage is an estimate somebody typed."""
    finished_but_stale = Task(
        "A",
        "A",
        5,
        "5D",
        actual_start=date(2026, 6, 1),
        actual_finish=date(2026, 6, 5),
        percent_complete=40.0,
    )
    result = measure(
        [finished_but_stale, todo("B")], BASELINE, data_date=date(2026, 6, 8), calendar=five_day
    )
    assert result.earned_duration_days == pytest.approx(5.0)


# -- what it refuses -------------------------------------------------------


def test_no_time_elapsed_is_none_rather_than_perfect(five_day) -> None:  # type: ignore[no-untyped-def]
    """The rule `progress.py` applies to BEI, applied here.

    A ratio with a zero denominator is not perfect performance, it is no
    information -- and reporting it as 1.0 puts a green tile on a dashboard
    for a project that has not started.
    """
    result = measure(
        [todo("A"), todo("B")], BASELINE, data_date=date(2026, 6, 1), calendar=five_day
    )
    assert result.actual_time_days == 0
    assert result.performance_index is None
    assert result.to_dict()["performance_index"] is None
    assert result.to_dict()["is_behind"] is False


def test_a_schedule_with_no_baseline_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(EarnedScheduleError, match="no activity has a baseline"):
        measure([todo("A")], {}, data_date=date(2026, 6, 8), calendar=five_day)


def test_scope_added_after_the_baseline_counts_on_neither_side(five_day) -> None:  # type: ignore[no-untyped-def]
    """Unplanned work must not inflate the index.

    An activity the baseline never contained has no planned position to be
    measured against. Counting its progress rewards a project for doing work
    nobody asked for, and counting it as zero penalises one for the same.
    Excluded from both sides is the only answer that does neither.
    """
    with_extra = [
        done("A", date(2026, 6, 1), date(2026, 6, 5)),
        todo("B"),
        done("VARIATION", date(2026, 6, 1), date(2026, 6, 5)),
    ]
    without = [done("A", date(2026, 6, 1), date(2026, 6, 5)), todo("B")]

    a = measure(with_extra, BASELINE, data_date=date(2026, 6, 8), calendar=five_day)
    b = measure(without, BASELINE, data_date=date(2026, 6, 8), calendar=five_day)

    assert a.performance_index == b.performance_index
    assert a.earned_duration_days == b.earned_duration_days
    assert a.baseline_duration_days == b.baseline_duration_days


def test_earned_time_cannot_exceed_the_baseline_it_is_measured_on(five_day) -> None:  # type: ignore[no-untyped-def]
    """The cap that keeps a late completion from drifting back to 1.0.

    Everything planned is earned, so there is no more baseline curve to be
    further along. Without the cap, extra elapsed time would keep raising ES
    and the index would climb back toward 1.0 exactly as the classic one does.
    """
    tasks = [
        done("A", date(2026, 6, 1), date(2026, 6, 5)),
        done("B", date(2026, 6, 8), date(2026, 6, 12)),
    ]
    early = measure(tasks, BASELINE, data_date=date(2026, 6, 15), calendar=five_day)
    late = measure(tasks, BASELINE, data_date=date(2026, 7, 13), calendar=five_day)

    assert early.earned_days == late.earned_days == 10.0
    assert early.performance_index == pytest.approx(1.0)
    assert late.performance_index is not None
    assert late.performance_index < 0.5, "more elapsed time on the same work is worse, not better"


def test_the_result_is_json_safe(five_day) -> None:  # type: ignore[no-untyped-def]
    import json

    result = measure(
        [done("A", date(2026, 6, 1), date(2026, 6, 5)), todo("B")],
        BASELINE,
        data_date=date(2026, 6, 8),
        calendar=five_day,
    )
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["planned_duration_days"] == 10
    assert payload["performance_index"] == 1.0


# -- reachable through the API ---------------------------------------------


def test_the_api_endpoint_measures_and_refuses() -> None:
    """Reachable, and the refusals survive the trip out as validation errors."""
    from massingplan.api.errors import ValidationFailed
    from massingplan.api.schedules import measure_earned_schedule

    body = {
        "data_date": "2026-06-29",
        "activities": [
            {
                "id": "A",
                "duration_days": 5,
                "actual_start": "2026-06-01",
                "actual_finish": "2026-06-12",
            },
            {
                "id": "B",
                "duration_days": 5,
                "predecessors": ["A"],
                "actual_start": "2026-06-15",
                "actual_finish": "2026-06-26",
            },
        ],
        "baseline": {"A": ["2026-06-01", "2026-06-05"], "B": ["2026-06-08", "2026-06-12"]},
    }
    result = measure_earned_schedule(body)
    assert result["performance_index"] == 0.5
    assert result["classic_performance_index"] == 1.0, "the pair is the argument"
    assert result["is_behind"] is True

    with pytest.raises(ValidationFailed, match="data_date"):
        measure_earned_schedule({k: v for k, v in body.items() if k != "data_date"})

    with pytest.raises(ValidationFailed, match="baseline"):
        measure_earned_schedule({**body, "baseline": {}})

    with pytest.raises(ValidationFailed, match=r"start, finish"):
        measure_earned_schedule({**body, "baseline": {"A": ["2026-06-01"]}})


def test_the_capability_listing_names_the_new_analyses() -> None:
    """`capabilities` exists so nobody discovers a feature by trial.

    A feature missing from it is exactly as invisible as one that does not
    exist, which makes the listing worse than useless once it drifts.
    """
    from massingplan.app import create_app
    from massingplan.blueprints.schedule_api import capabilities

    app = create_app()
    with app.test_request_context():
        payload = capabilities().get_json()

    assert "earned_schedule" in payload["features"]
    assert any("windows" in f for f in payload["features"])
