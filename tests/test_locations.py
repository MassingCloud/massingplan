"""Location-based scheduling, checked with a pencil.

Every expected number here was worked out by hand and written into the test,
not produced by running the code and pasting the answer. A line-of-balance
result is a picture of where crews are, and a picture that is confidently wrong
is worse than no picture.
"""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.locations import (
    LinearScheduleError,
    LinearTask,
    Location,
    compute,
    to_network,
)
from massingplan.core.schedule import schedule_network
from massingplan.core.timeaxis import WorkCalendar, WorkPattern

FLOORS = [Location(f"L{i}", f"Level {i}", sequence=i) for i in range(1, 5)]


def _task(tid: str, days: int, buffer_days: int = 0, **kw: object) -> LinearTask:
    return LinearTask(id=tid, duration_days=days, buffer_days=buffer_days, **kw)  # type: ignore[arg-type]


def _offsets(result, task_id: str) -> list[tuple[int, int]]:  # type: ignore[no-untyped-def]
    return [(s.start_offset, s.finish_offset) for s in result.by_task(task_id)]


# -- continuity, which is the whole point ----------------------------------


def test_one_crew_flows_through_the_locations_without_a_gap() -> None:
    """Four floors, two days each. The gang works days 0-1, 2-3, 4-5, 6-7 with
    no break: each location starts the instant the previous one ends.
    """
    result = compute([_task("FRAME", 2)], FLOORS)
    assert _offsets(result, "FRAME") == [(0, 2), (2, 4), (4, 6), (6, 8)]
    assert result.duration_days == 8


def test_a_following_trade_waits_for_the_space_and_then_never_stops() -> None:
    """FRAME takes 2 days a floor, DRYWALL takes 2. Same pace, so the buffer is
    constant and DRYWALL simply starts one floor behind: L1 on day 2.
    """
    result = compute([_task("FRAME", 2), _task("DRYWALL", 2)], FLOORS)
    assert _offsets(result, "DRYWALL") == [(2, 4), (4, 6), (6, 8), (8, 10)]
    assert result.duration_days == 10


def test_the_slower_successor_is_held_by_the_first_location() -> None:
    """FRAME 2 days a floor, MEP 4. The successor loses ground as it climbs, so
    the binding constraint is the *first* floor: MEP starts at day 2 and the gap
    only widens after that.
    """
    result = compute([_task("FRAME", 2), _task("MEP", 4)], FLOORS)
    assert _offsets(result, "MEP") == [(2, 6), (6, 10), (10, 14), (14, 18)]
    interference = result.interferences[0]
    assert interference.location_id == "L1"
    assert interference.gap_days == 0
    assert interference.converging is False


def test_the_faster_successor_is_held_by_the_last_location() -> None:
    """This is the case a Gantt chart cannot show and the reason the line shift
    takes a maximum over every location rather than just the first.

    FRAME takes 4 days a floor and finishes L4 on day 16. PAINT takes 1 day a
    floor. Starting PAINT as soon as L1 is free (day 4) would put it in L4 on
    day 7 -- nine days before FRAME leaves. So the whole PAINT line shifts right
    to 16 - 3 = 13, and it is the top floor, not the bottom, that fixes it.
    """
    result = compute([_task("FRAME", 4), _task("PAINT", 1)], FLOORS)
    assert _offsets(result, "PAINT") == [(13, 14), (14, 15), (15, 16), (16, 17)]

    interference = result.interferences[0]
    assert interference.location_id == "L4"
    assert interference.gap_days == 0
    assert interference.converging is True, "a faster successor converges on its predecessor"


def test_taking_only_the_first_location_would_put_the_trades_in_the_same_room() -> None:
    """Guard the guard: prove the naive shift is wrong, so the maximum in
    `compute` cannot be simplified away by someone who has not met this case.
    """
    result = compute([_task("FRAME", 4), _task("PAINT", 1)], FLOORS)
    frame = {s.location_id: s for s in result.by_task("FRAME")}
    paint = {s.location_id: s for s in result.by_task("PAINT")}
    for location in FLOORS:
        assert paint[location.id].start_offset >= frame[location.id].finish_offset, location.id

    naive_start = frame["L1"].finish_offset  # what "start when L1 is free" gives
    assert naive_start == 4
    assert paint["L1"].start_offset == 13, "the line must shift past the naive answer"


# -- buffers ---------------------------------------------------------------


def test_a_buffer_holds_the_following_trade_off_by_that_many_days() -> None:
    result = compute([_task("FRAME", 2), _task("DRYWALL", 2, buffer_days=3)], FLOORS)
    assert _offsets(result, "DRYWALL") == [(5, 7), (7, 9), (9, 11), (11, 13)]
    assert result.interferences[0].gap_days == 3


def test_a_negative_buffer_is_a_deliberate_overlap_and_is_reported() -> None:
    """Legal, occasionally correct, and never silent. Two trades sharing a floor
    is a decision somebody should have to defend.
    """
    result = compute([_task("FRAME", 4), _task("DRYWALL", 4, buffer_days=-2)], FLOORS)
    assert result.interferences[0].gap_days == -2
    assert "LOB.OVERLAP" in result.issues.codes()


# -- quantities and production rates ---------------------------------------


def test_duration_comes_from_quantity_over_rate() -> None:
    """380 m2 at 95 m2/day is 4 days, and 200 at 95 is 3 -- rounded up, because
    two-thirds of a crew-day is a day on site.
    """
    task = LinearTask(
        id="SLAB",
        quantities={"L1": 380.0, "L2": 200.0, "L3": 95.0, "L4": 96.0},
        rate=95.0,
    )
    result = compute([task], FLOORS)
    assert [s.duration_days for s in result.by_task("SLAB")] == [4, 3, 1, 2]


def test_the_rounding_epsilon_stops_a_four_day_slab_becoming_five() -> None:
    """`380 / 95` is not exactly 4.0 in binary, and a bare `ceil` would make it
    five. This is the same trap `units.py` exists to hold shut.
    """
    task = LinearTask(id="SLAB", quantities={loc.id: 380.0 for loc in FLOORS}, rate=95.0)
    result = compute([task], FLOORS)
    assert [s.duration_days for s in result.by_task("SLAB")] == [4, 4, 4, 4]


def test_a_quantity_with_no_rate_falls_back_and_says_so() -> None:
    task = LinearTask(id="SLAB", duration_days=3, quantities={"L1": 380.0})
    result = compute([task], FLOORS)
    assert result.by_task("SLAB")[0].duration_days == 3
    assert "LOB.NO_RATE" in result.issues.codes()


def test_a_zero_rate_is_refused_rather_than_dividing_by_it() -> None:
    with pytest.raises(LinearScheduleError, match="positive"):
        LinearTask(id="SLAB", quantities={"L1": 10.0}, rate=0.0)


# -- the price of continuity -----------------------------------------------


def test_the_continuity_cost_is_reported_not_absorbed() -> None:
    """The number that decides whether location-based scheduling is worth it
    here: how much later the crew had to start so it would not be fragmented.

    PAINT could have entered L1 on day 4 and was held to day 13, so continuity
    cost nine days. That is a real trade -- nine days of float given up to keep
    one gang on site continuously -- and the planner should see the price.
    """
    result = compute([_task("FRAME", 4), _task("PAINT", 1)], FLOORS)
    assert result.continuity_cost_days["PAINT"] == 9
    assert result.continuity_cost_days["FRAME"] == 0


def test_matched_pace_costs_nothing() -> None:
    result = compute([_task("FRAME", 2), _task("DRYWALL", 2)], FLOORS)
    assert result.continuity_cost_days["DRYWALL"] == 0


# -- limits that are stated rather than guessed ----------------------------


def test_more_than_one_crew_is_refused_loudly_rather_than_approximated() -> None:
    result = compute([_task("FRAME", 2, crews=3)], FLOORS)
    assert "LOB.CREWS_NOT_MODELLED" in result.issues.codes()
    assert _offsets(result, "FRAME") == [(0, 2), (2, 4), (4, 6), (6, 8)]


def test_mixed_calendars_are_reported_because_the_arithmetic_is_in_days() -> None:
    result = compute(
        [_task("FRAME", 2), LinearTask(id="MEP", duration_days=2, calendar_id="6D")], FLOORS
    )
    assert "LOB.MIXED_CALENDARS" in result.issues.codes()


def test_locations_flow_in_sequence_order_not_list_order() -> None:
    shuffled = [FLOORS[2], FLOORS[0], FLOORS[3], FLOORS[1]]
    result = compute([_task("FRAME", 2)], shuffled)
    assert [s.location_id for s in result.by_task("FRAME")] == ["L1", "L2", "L3", "L4"]


def test_a_duplicate_location_is_an_error_not_a_silent_overwrite() -> None:
    with pytest.raises(LinearScheduleError, match="duplicate"):
        compute([_task("FRAME", 2)], [*FLOORS, Location("L2", "again", sequence=9)])


def test_an_empty_model_says_which_half_is_missing() -> None:
    with pytest.raises(LinearScheduleError, match="task"):
        compute([], FLOORS)
    with pytest.raises(LinearScheduleError, match="location"):
        compute([_task("FRAME", 2)], [])


# -- dates, at the one conversion site -------------------------------------


def test_offsets_become_dates_on_the_calendar_and_finish_is_the_last_day_worked() -> None:
    """1 June 2026 is a Monday. Two working days is Mon-Tue, so the first
    location finishes on the 2nd, not the 3rd -- the half-open boundary is
    converted once and displayed as the last day worked.

    The third location starts at offset 4, which is the *fifth* working day:
    Mon 1, Tue 2, Wed 3, Thu 4, **Fri 5**. Its boundary is offset 6 = Tue 9, and
    the last day worked is Mon 8 -- not Sunday the 7th, which is what
    subtracting one calendar day from the boundary would print.
    """
    result = compute([_task("FRAME", 2)], FLOORS)
    rows = result.to_rows(start=date(2026, 6, 1), calendar=_five_day())
    assert rows[0]["start"] == "2026-06-01"
    assert rows[0]["finish"] == "2026-06-02"
    assert rows[2]["start"] == "2026-06-05"
    assert rows[2]["finish"] == "2026-06-08"


def test_a_weekend_is_skipped_because_the_flow_is_in_working_days() -> None:
    """The top floor starts at offset 6 -- the seventh working day, Tue 9 June,
    because the weekend of the 6th and 7th is not worked. A location-based
    schedule that counted calendar days would have the gang on site that
    Saturday.
    """
    result = compute([_task("FRAME", 2)], FLOORS)
    rows = result.to_rows(start=date(2026, 6, 1), calendar=_five_day())
    assert rows[3]["start"] == "2026-06-09"
    assert rows[3]["finish"] == "2026-06-10"


def _five_day() -> WorkCalendar:
    cal = WorkCalendar("5D", "Mon-Fri", WorkPattern(frozenset({0, 1, 2, 3, 4})))
    cal.bind(date(2025, 1, 1), date(2030, 12, 31))
    return cal


# -- it becomes an ordinary network ----------------------------------------


def test_the_emitted_network_reproduces_the_line() -> None:
    """The point of emitting rather than scheduling separately: the existing
    engine has to arrive at the same answer, or location-based scheduling is a
    second product hiding inside the first.
    """
    tasks = [_task("FRAME", 4), _task("PAINT", 1)]
    result = compute(tasks, FLOORS)
    net_tasks, links, calendars = to_network(
        result, tasks, FLOORS, start=date(2026, 6, 1), calendar=_five_day()
    )
    outcome = schedule_network(net_tasks, links, calendars, data_date=date(2026, 6, 1))

    computed = {
        row["activity_id"]: row
        for row in result.to_rows(start=date(2026, 6, 1), calendar=_five_day())
    }
    for row in outcome.to_rows():
        expected = computed[str(row["activity_id"])]
        assert str(row["start"]) == expected["start"], row["activity_id"]
        assert str(row["finish"]) == expected["finish"], row["activity_id"]


def test_the_emitted_network_carries_both_kinds_of_logic() -> None:
    """Crew continuity and handover are different constraints and both have to
    be in the graph: continuity keeps the gang together, handover keeps the
    trades apart.
    """
    tasks = [_task("FRAME", 2), _task("DRYWALL", 2, buffer_days=1)]
    result = compute(tasks, FLOORS)
    _net_tasks, links, _cals = to_network(result, tasks, FLOORS, start=date(2026, 6, 1))
    pairs = {(link.predecessor, link.successor): link for link in links}

    continuity = pairs[("FRAME@L1", "FRAME@L2")]
    assert continuity.lag_days == 0, "a crew moves on with no gap"

    handover = pairs[("FRAME@L1", "DRYWALL@L1")]
    assert handover.lag_days == 1, "the buffer travels as the lag"


def test_every_emitted_activity_is_named_for_a_human() -> None:
    tasks = [LinearTask(id="FRAME", name="Framing", duration_days=2)]
    result = compute(tasks, FLOORS)
    net_tasks, _links, _cals = to_network(result, tasks, FLOORS, start=date(2026, 6, 1))
    assert net_tasks[0].id == "FRAME@L1"
    assert "Framing" in net_tasks[0].name and "L1" in net_tasks[0].name


def test_a_forty_floor_tower_stays_quick() -> None:
    """Twelve trades over forty floors is 480 activities, which is an ordinary
    tower and must not be a slow case.
    """
    import time

    floors = [Location(f"L{i}", sequence=i) for i in range(1, 41)]
    trades = [_task(f"T{i}", (i % 4) + 1, buffer_days=1) for i in range(12)]
    began = time.perf_counter()
    result = compute(trades, floors)
    elapsed = time.perf_counter() - began
    assert len(result.segments) == 480
    assert elapsed < 1.0, f"480 segments took {elapsed:.2f}s"


# -- through the API -------------------------------------------------------


def _linear_payload() -> dict:
    return {
        "start": "2026-06-01",
        "locations": [{"id": f"L{i}", "sequence": i} for i in range(1, 5)],
        "tasks": [
            {"id": "FRAME", "name": "Framing", "duration_days": 4},
            {"id": "PAINT", "name": "Painting", "duration_days": 1},
        ],
    }


def test_the_api_returns_the_flow_and_the_network_it_emits() -> None:
    from massingplan.api import schedules

    result = schedules.schedule_linear(_linear_payload())
    assert result["duration_working_days"] == 17
    assert len(result["segments"]) == 8
    assert result["continuity_cost_days"]["PAINT"] == 9

    binding = result["interferences"][0]
    assert binding["location_id"] == "L4"
    assert binding["converging"] is True


def test_the_emitted_activities_schedule_to_the_same_dates_through_the_normal_endpoint() -> None:
    """The whole design in one assertion: location-based scheduling emits a
    network, and the ordinary scheduler agrees with it. If these ever diverge,
    there are two schedulers in the product and one of them is lying.
    """
    from massingplan.api import schedules

    linear = schedules.schedule_linear(_linear_payload())
    replayed = schedules.schedule_from_payload(
        {"data_date": "2026-06-01", "activities": linear["activities"]}
    )

    flow = {row["activity_id"]: row for row in linear["segments"]}
    for row in replayed["activities"]:
        expected = flow[str(row["activity_id"])]
        assert str(row["start"]) == expected["start"], row["activity_id"]
        assert str(row["finish"]) == expected["finish"], row["activity_id"]


def test_a_model_with_no_locations_is_refused_with_a_reason() -> None:
    from massingplan.api import schedules
    from massingplan.api.errors import ValidationFailed

    with pytest.raises(ValidationFailed, match="location"):
        schedules.schedule_linear({"start": "2026-06-01", "tasks": [{"id": "A"}]})


def test_the_capability_list_now_names_it() -> None:
    """A feature nobody can discover is a feature nobody uses -- and this list
    is the one place a client is told what the build can do.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "massingplan" / "blueprints" / "schedule_api.py"
    ).read_text(encoding="utf-8")
    assert "location_based_scheduling" in source
