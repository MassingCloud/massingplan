"""The hub model and the presentation layer.

The tests that matter here are about the *convention*: inclusive finish dates,
milestone asymmetry, a frozen row contract, and not mutating the caller's object.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from massingplan.core.constraints import ConstraintType
from massingplan.core.model import (
    Calendar,
    CalendarException,
    ExchangeActivity,
    ExchangeAssignment,
    ExchangeRelationship,
    ExchangeResource,
    ExchangeSchedule,
    WBSNode,
)
from massingplan.core.network import ActivityKind, RelationType, SchedulerOptions, Task
from massingplan.core.schedule import ROW_KEYS, schedule, schedule_network

JUN1 = date(2026, 6, 1)


def build() -> ExchangeSchedule:
    """A four-activity mid-rise fragment, on two calendars."""
    return ExchangeSchedule(
        project_id="DEMO",
        project_name="Mid-rise fragment",
        data_date=JUN1,
        planned_start=JUN1,
        default_calendar_id="5D",
        source_format="fixture",
        calendars=[
            Calendar("5D", "Mon-Fri", {0, 1, 2, 3, 4}, is_default=True),
            Calendar("6D", "Mon-Sat", {0, 1, 2, 3, 4, 5}),
        ],
        wbs=[WBSNode("W1", "Substructure"), WBSNode("W2", "Superstructure", parent_id="W1")],
        activities=[
            ExchangeActivity(
                "A", "Excavate", wbs_id="W1", calendar_id="5D", duration_days=5, code="A1010"
            ),
            ExchangeActivity(
                "B", "Foundations", wbs_id="W1", calendar_id="5D", duration_days=10, code="A1020"
            ),
            ExchangeActivity(
                "C", "Steel", wbs_id="W2", calendar_id="6D", duration_days=8, code="A1030"
            ),
            ExchangeActivity(
                "M",
                "Topping out",
                wbs_id="W2",
                calendar_id="5D",
                kind=ActivityKind.FINISH_MILESTONE,
                code="M1000",
            ),
        ],
        relationships=[
            ExchangeRelationship("A", "B"),
            ExchangeRelationship("B", "C"),
            ExchangeRelationship("C", "M"),
        ],
        resources=[ExchangeResource("R1", "Carpenters", unit="crew", unit_cost=650.0)],
        assignments=[ExchangeAssignment("B", "R1", units_per_day=2.0)],
    )


# -- the model -------------------------------------------------------------


def test_a_sound_schedule_validates_clean() -> None:
    assert build().validate() == []


def test_validate_reports_every_problem_at_once_not_the_first() -> None:
    """Fix, re-run, fix, re-run is a worse conversation than one list."""
    s = build()
    s.activities.append(ExchangeActivity("A", "Duplicate", duration_days=1))
    s.activities.append(ExchangeActivity("X", "Bad calendar", calendar_id="GHOST"))
    s.relationships.append(ExchangeRelationship("A", "NOWHERE"))
    s.assignments.append(ExchangeAssignment("A", "NO_SUCH_RESOURCE"))
    problems = s.validate()
    assert len(problems) >= 4
    assert any("duplicate activity id" in p for p in problems)
    assert any("unknown calendar" in p for p in problems)
    assert any("unknown successor" in p for p in problems)
    assert any("unknown resource" in p for p in problems)


def test_to_network_carries_the_things_to_cpm_used_to_drop() -> None:
    """The deleted ``to_cpm()`` lost calendars, constraints and actuals.

    That loss is the defect this engine exists to fix, so its replacement is
    tested for exactly those three.
    """
    s = build()
    s.activities[0].constraint = ConstraintType.START_ON_OR_AFTER
    s.activities[0].constraint_date = date(2026, 6, 8)
    s.activities[1].actual_start = date(2026, 6, 2)
    s.activities[1].remaining_duration_days = 4

    tasks, links, calendars = s.to_network()
    by_id = {t.id: t for t in tasks}

    assert by_id["C"].calendar_id == "6D"
    assert by_id["A"].constraint is ConstraintType.START_ON_OR_AFTER
    assert by_id["A"].constraint_date == date(2026, 6, 8)
    assert by_id["B"].actual_start == date(2026, 6, 2)
    assert by_id["B"].remaining_days == 4
    assert set(calendars) == {"5D", "6D"}
    assert len(links) == 3


def test_to_cpm_is_gone_so_nobody_can_reach_for_it() -> None:
    assert not hasattr(ExchangeSchedule, "to_cpm")


def test_summary_bars_are_not_scheduled_as_work() -> None:
    """A summary is a rollup of its children; scheduling it double-counts them."""
    s = build()
    s.activities.append(
        ExchangeActivity("SUM", "Substructure", kind=ActivityKind.WBS_SUMMARY, duration_days=15)
    )
    tasks, _links, _cals = s.to_network()
    assert "SUM" not in {t.id for t in tasks}


def test_a_calendar_exception_can_add_a_day_as_well_as_remove_one() -> None:
    cal = Calendar(
        "C",
        "Mon-Fri with a make-up Saturday",
        {0, 1, 2, 3, 4},
        exceptions=[
            CalendarException(date(2026, 12, 25), working=False, name="Christmas"),
            CalendarException(date(2026, 6, 6), working=True, name="Make-up"),
        ],
    )
    work = cal.to_work_calendar()
    work.bind(date(2026, 1, 1), date(2027, 1, 31))
    from massingplan.core.timeaxis import instant_of

    assert work.is_working(instant_of(date(2026, 6, 6))) is True
    assert work.is_working(instant_of(date(2026, 12, 25))) is False


def test_physical_percent_beats_duration_percent() -> None:
    """On a stalled activity, elapsed time and work done diverge."""
    a = ExchangeActivity("A", duration_percent_complete=0.8, physical_percent_complete=0.3)
    assert a.percent_complete == 0.3


def test_a_zero_hours_per_day_calendar_is_refused() -> None:
    with pytest.raises(ValueError, match="hours_per_day must be positive"):
        Calendar("BAD", hours_per_day=0)


def test_summary_counts_by_kind_and_type() -> None:
    s = build().summary()
    assert s["activities"] == 4
    assert s["relationships_by_type"] == {"FS": 3}
    assert s["activities_by_kind"]["finish_milestone"] == 1


# -- presentation ----------------------------------------------------------


def test_a_five_day_activity_finishes_on_the_friday_it_last_worked(five_day) -> None:  # type: ignore[no-untyped-def]
    """The inclusive-finish convention, applied at exactly one site."""
    out = schedule_network([Task("A", "", 5, "5D")], [], {"5D": five_day}, data_date=JUN1)
    assert out.dates["A"].start == date(2026, 6, 1)
    assert out.dates["A"].finish == date(2026, 6, 5)


def test_a_one_day_activity_starts_and_finishes_the_same_day(five_day) -> None:  # type: ignore[no-untyped-def]
    out = schedule_network([Task("A", "", 1, "5D")], [], {"5D": five_day}, data_date=JUN1)
    assert out.dates["A"].start == out.dates["A"].finish == date(2026, 6, 1)


def test_a_finish_milestone_shows_on_the_day_the_work_ended_not_the_day_after(
    five_day,
) -> None:  # type: ignore[no-untyped-def]
    """Substantial Completion must not print on the Monday after the Friday.

    The milestone's instant is the boundary *after* the last work, so presenting
    it naively puts every contractual date a day late.
    """
    from massingplan.core.network import Link

    tasks = [
        Task("WORK", "", 5, "5D"),
        Task("SC", "Substantial completion", 0, "5D", kind=ActivityKind.FINISH_MILESTONE),
    ]
    out = schedule_network(tasks, [Link("WORK", "SC")], {"5D": five_day}, data_date=JUN1)
    assert out.dates["WORK"].finish == date(2026, 6, 5)
    assert out.dates["SC"].start == date(2026, 6, 5)
    assert out.dates["SC"].finish == date(2026, 6, 5)


def test_a_start_milestone_shows_on_its_own_start(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks = [Task("NTP", "Notice to proceed", 0, "5D", kind=ActivityKind.START_MILESTONE)]
    out = schedule_network(tasks, [], {"5D": five_day}, data_date=JUN1)
    assert out.dates["NTP"].start == date(2026, 6, 1)


# -- the persistence contract ----------------------------------------------


def test_the_row_key_set_is_frozen() -> None:
    """A rename in the engine must fail here rather than dropping a column
    silently in whatever consumes these rows.
    """
    out = schedule(build())
    row = out.to_rows()[0]
    assert set(row) == ROW_KEYS


def test_rows_are_json_serialisable_with_no_custom_encoder() -> None:
    out = schedule(build())
    json.dumps(out.to_rows())
    json.dumps(out.summary())


def test_rows_come_back_in_topological_order() -> None:
    out = schedule(build())
    assert [r["activity_id"] for r in out.to_rows()] == ["A", "B", "C", "M"]


def test_apply_to_does_not_mutate_its_argument() -> None:
    """A failed write must not leave a half-updated schedule in memory."""
    source = build()
    out = schedule(source)
    updated = source.apply_to(out) if False else out.apply_to(source)

    assert source.activities[0].early_start is None
    assert updated.activities[0].early_start == date(2026, 6, 1)
    assert updated is not source
    assert updated.activities[0] is not source.activities[0]


def test_apply_to_writes_float_and_longest_path_membership() -> None:
    out = schedule(build())
    updated = out.apply_to(build())
    by_id = {a.id: a for a in updated.activities}
    assert by_id["A"].total_float_days == 0
    assert by_id["A"].is_longest_path is True


def test_the_schedule_runs_across_two_calendars() -> None:
    """C is on Mon-Sat, so its eight days finish sooner than they would on Mon-Fri."""
    out = schedule(build())
    # A: 1-5 Jun (5d, Mon-Fri). B: 8-19 Jun (10d). C starts Sat 20 Jun on Mon-Sat.
    assert out.dates["A"].finish == date(2026, 6, 5)
    assert out.dates["B"].finish == date(2026, 6, 19)
    assert out.dates["C"].start == date(2026, 6, 20)


def test_must_finish_by_is_picked_up_from_the_schedule_when_options_are_silent() -> None:
    source = build()
    source.must_finish_by = date(2026, 6, 10)
    out = schedule(source)
    assert out.options.must_finish_by == date(2026, 6, 10)
    assert out.dates["C"].total_float_days is not None
    assert out.dates["C"].total_float_days < 0


def test_explicit_options_win_over_the_schedule() -> None:
    source = build()
    source.must_finish_by = date(2026, 6, 10)
    out = schedule(source, options=SchedulerOptions(must_finish_by=date(2027, 1, 1)))
    assert out.options.must_finish_by == date(2027, 1, 1)


def test_relationship_types_survive_the_round_trip_into_the_network() -> None:
    s = build()
    s.relationships[1].type = RelationType.SS
    s.relationships[1].lag_days = 3
    _tasks, links, _cals = s.to_network()
    ss = next(link for link in links if link.predecessor == "B")
    assert ss.type is RelationType.SS
    assert ss.lag_days == 3
