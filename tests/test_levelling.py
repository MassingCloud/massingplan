"""Resource profiles and deterministic levelling.

The textbook case is worked by hand in the first test; everything after it
defends a property the levelling result has to have to be usable as evidence.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date

from massingplan.core.levelling import (
    DEFAULT_PRIORITY,
    LevellingHorizon,
    LevellingMode,
    LevellingRequest,
    default_priority,
    level,
)
from massingplan.core.network import Link, Task
from massingplan.core.resources import (
    Demand,
    ResourceAvailability,
    daily_profile,
    histogram,
    over_allocations,
)
from massingplan.core.schedule import schedule_network

JUN1 = date(2026, 6, 1)


def two_activities_one_crew(five_day):  # type: ignore[no-untyped-def]
    """A(3) and B(3) both start Mon 1 June and both want the only crew."""
    tasks = [Task("A", "Level 1 walls", 3, "5D"), Task("B", "Level 2 walls", 3, "5D")]
    outcome = schedule_network(tasks, [], {"5D": five_day}, data_date=JUN1)
    demands = [Demand("A", "CREW", 1.0), Demand("B", "CREW", 1.0)]
    availability = [ResourceAvailability("CREW", units_per_day=1.0)]
    return tasks, outcome, demands, availability


# -- profiles --------------------------------------------------------------


def test_demand_is_units_per_day_held_level_not_spread(five_day) -> None:  # type: ignore[no-untyped-def]
    """A three-day activity at one crew per day demands one crew on each of
    three days -- not one third of a crew for nine.

    Linear spreading makes peak demand a function of duration, which turns
    levelling non-monotonic: it can raise the peak it was asked to lower.
    """
    spans = {"A": (date(2026, 6, 1), date(2026, 6, 3))}
    profile = daily_profile(spans, [Demand("A", "CREW", 2.0)], {"5D": five_day}, {"A": "5D"})
    assert profile["CREW"] == {
        date(2026, 6, 1): 2.0,
        date(2026, 6, 2): 2.0,
        date(2026, 6, 3): 2.0,
    }


def test_the_profile_skips_non_working_days(five_day) -> None:  # type: ignore[no-untyped-def]
    spans = {"A": (date(2026, 6, 5), date(2026, 6, 9))}  # Fri to Tue
    profile = daily_profile(spans, [Demand("A", "CREW", 1.0)], {"5D": five_day}, {"A": "5D"})
    assert set(profile["CREW"]) == {date(2026, 6, 5), date(2026, 6, 8), date(2026, 6, 9)}


def test_completed_work_consumes_nothing(five_day) -> None:  # type: ignore[no-untyped-def]
    """Counting last month's crew against this month blocks all future work."""
    spans = {"DONE": (date(2026, 6, 1), date(2026, 6, 3))}
    profile = daily_profile(
        spans,
        [Demand("DONE", "CREW", 1.0)],
        {"5D": five_day},
        {"DONE": "5D"},
        exclude=["DONE"],
    )
    assert profile == {}


def test_over_allocation_is_detected_per_day_and_names_the_contributors(five_day) -> None:  # type: ignore[no-untyped-def]
    spans = {
        "A": (date(2026, 6, 1), date(2026, 6, 3)),
        "B": (date(2026, 6, 2), date(2026, 6, 4)),
    }
    clashes = over_allocations(
        spans,
        [Demand("A", "CREW", 1.0), Demand("B", "CREW", 1.0)],
        [ResourceAvailability("CREW", 1.0)],
        {"5D": five_day},
        {"A": "5D", "B": "5D"},
    )
    days = {c.day for c in clashes}
    assert days == {date(2026, 6, 2), date(2026, 6, 3)}
    assert clashes[0].contributors == ("A", "B")
    assert clashes[0].excess == 1.0


def test_weekly_bucketing_still_reports_the_monday_spike(five_day) -> None:  # type: ignore[no-untyped-def]
    """Bucketing first hides a 3x Monday inside a week that averages under cap."""
    spans = {f"A{i}": (date(2026, 6, 1), date(2026, 6, 1)) for i in range(3)}
    demands = [Demand(f"A{i}", "CREW", 1.0) for i in range(3)]
    cals, act_cal = {"5D": five_day}, {f"A{i}": "5D" for i in range(3)}

    profile = daily_profile(spans, demands, cals, act_cal)
    rows = histogram(profile, bucket="week")
    assert rows[0]["total_units"] == 3.0
    assert rows[0]["peak_day_units"] == 3.0

    clashes = over_allocations(spans, demands, [ResourceAvailability("CREW", 1.0)], cals, act_cal)
    assert len(clashes) == 1
    assert clashes[0].demanded == 3.0


def test_a_resource_on_its_own_calendar_is_unavailable_not_over_allocated(
    five_day, seven_day
) -> None:  # type: ignore[no-untyped-def]
    """Work scheduled on a Sunday against a Mon-Fri crew is zero availability."""
    spans = {"A": (date(2026, 6, 6), date(2026, 6, 7))}  # Sat and Sun
    clashes = over_allocations(
        spans,
        [Demand("A", "CREW", 1.0)],
        [ResourceAvailability("CREW", 1.0, calendar_id="5D")],
        {"5D": five_day, "7D": seven_day},
        {"A": "7D"},
    )
    assert len(clashes) == 2
    assert all(c.available == 0.0 for c in clashes)


# -- levelling -------------------------------------------------------------


def test_the_textbook_case_by_hand(five_day) -> None:  # type: ignore[no-untyped-def]
    """Two three-day activities, one crew, no logic between them.

    Both want Mon 1 to Wed 3 June. One of them has to move to Thu 4 to Mon 8.
    Priority is ``(late_start, total_float, -duration, id)``; both have the same
    late start, float and duration, so the id decides and A goes first.
    """
    tasks, outcome, demands, availability = two_activities_one_crew(five_day)
    result = level(
        LevellingRequest(
            outcome=outcome,
            tasks=tasks,
            links=[],
            calendars={"5D": five_day},
            demands=demands,
            availability=availability,
            horizon=LevellingHorizon.EXTEND_FINISH,
        )
    )
    assert len(result.moves) == 1
    move = result.moves[0]
    assert move.activity_id == "B"
    assert move.from_start == date(2026, 6, 1)
    assert move.to_start == date(2026, 6, 4)
    assert move.blocked_by == ("CREW",)
    assert result.unresolved == ()
    assert result.peak_before["CREW"] == 2.0
    assert result.peak_after["CREW"] == 1.0


def test_within_float_never_moves_the_finish_and_reports_what_it_could_not_solve(
    five_day,
) -> None:  # type: ignore[no-untyped-def]
    """Quietly extending a contract date nobody approved is the worst outcome
    available. A non-empty ``unresolved`` is the honest one.
    """
    tasks, outcome, demands, availability = two_activities_one_crew(five_day)
    result = level(
        LevellingRequest(
            outcome=outcome,
            tasks=tasks,
            links=[],
            calendars={"5D": five_day},
            demands=demands,
            availability=availability,
            horizon=LevellingHorizon.WITHIN_FLOAT,
        )
    )
    assert result.finish_after == result.finish_before
    assert result.finish_moved_days == 0
    assert result.unresolved != ()


def test_extend_finish_resolves_the_same_case_and_reports_the_move(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, outcome, demands, availability = two_activities_one_crew(five_day)
    result = level(
        LevellingRequest(
            outcome=outcome,
            tasks=tasks,
            links=[],
            calendars={"5D": five_day},
            demands=demands,
            availability=availability,
            horizon=LevellingHorizon.EXTEND_FINISH,
        )
    )
    assert result.unresolved == ()
    assert result.finish_after > result.finish_before


def test_an_activity_with_float_absorbs_the_clash_without_moving_the_finish(
    five_day,
) -> None:  # type: ignore[no-untyped-def]
    """The case levelling exists for: slack on a parallel branch soaks it up.

    STRUCTURE (10 days, a different trade) sets the project length. Two
    three-day joinery activities both want the single joinery crew and both sit
    on a branch with float. One slides three days and nothing else changes.

    Note the arithmetic has to actually work: a single crew cannot cover more
    crew-days than the float window holds, and a test that asks it to is
    asserting an impossibility rather than a property.
    """
    tasks = [
        Task("STRUCTURE", "Frame", 10, "5D"),
        Task("JOIN1", "Joinery A", 3, "5D"),
        Task("JOIN2", "Joinery B", 3, "5D"),
        Task("END", "Handover", 1, "5D"),
    ]
    links = [Link("STRUCTURE", "END"), Link("JOIN1", "END"), Link("JOIN2", "END")]
    outcome = schedule_network(tasks, links, {"5D": five_day}, data_date=JUN1)
    result = level(
        LevellingRequest(
            outcome=outcome,
            tasks=tasks,
            links=links,
            calendars={"5D": five_day},
            demands=[Demand("JOIN1", "CREW", 1.0), Demand("JOIN2", "CREW", 1.0)],
            availability=[ResourceAvailability("CREW", 1.0)],
            horizon=LevellingHorizon.WITHIN_FLOAT,
        )
    )
    assert result.finish_after == result.finish_before
    assert result.unresolved == ()
    assert [m.activity_id for m in result.moves] == ["JOIN2"]
    assert result.moves[0].to_start == date(2026, 6, 4)


def test_levelling_never_moves_an_activity_earlier(five_day) -> None:  # type: ignore[no-untyped-def]
    """It may only move work later. Earlier would break the logic CPM honoured."""
    tasks = [Task("A", "", 3, "5D"), Task("B", "", 3, "5D")]
    links = [Link("A", "B")]
    outcome = schedule_network(tasks, links, {"5D": five_day}, data_date=JUN1)
    result = level(
        LevellingRequest(
            outcome=outcome,
            tasks=tasks,
            links=links,
            calendars={"5D": five_day},
            demands=[Demand("A", "CREW", 1.0), Demand("B", "CREW", 1.0)],
            availability=[ResourceAvailability("CREW", 1.0)],
            horizon=LevellingHorizon.EXTEND_FINISH,
        )
    )
    for move in result.moves:
        assert move.to_start >= move.from_start


def test_an_in_progress_activity_is_pinned(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks = [
        Task("RUNNING", "", 5, "5D", actual_start=date(2026, 6, 1), remaining_days=3),
        Task("WAITING", "", 3, "5D"),
    ]
    outcome = schedule_network(tasks, [], {"5D": five_day}, data_date=date(2026, 6, 3))
    result = level(
        LevellingRequest(
            outcome=outcome,
            tasks=tasks,
            links=[],
            calendars={"5D": five_day},
            demands=[Demand("RUNNING", "CREW", 1.0), Demand("WAITING", "CREW", 1.0)],
            availability=[ResourceAvailability("CREW", 1.0)],
            horizon=LevellingHorizon.EXTEND_FINISH,
        )
    )
    assert "RUNNING" not in {m.activity_id for m in result.moves}


def test_advisory_and_applied_agree_on_the_moves_but_only_applied_returns_dates(
    five_day,
) -> None:  # type: ignore[no-untyped-def]
    tasks, outcome, demands, availability = two_activities_one_crew(five_day)

    def run(mode: LevellingMode):  # type: ignore[no-untyped-def]
        return level(
            LevellingRequest(
                outcome=outcome,
                tasks=tasks,
                links=[],
                calendars={"5D": five_day},
                demands=demands,
                availability=availability,
                horizon=LevellingHorizon.EXTEND_FINISH,
                mode=mode,
            )
        )

    advisory, applied = run(LevellingMode.ADVISORY), run(LevellingMode.APPLIED)
    assert [m.to_dict() for m in advisory.moves] == [m.to_dict() for m in applied.moves]
    assert advisory.spans == {}
    assert applied.spans != {}


def test_the_priority_rule_is_a_total_order() -> None:
    """Without the trailing id, hash order decides the answer."""
    assert DEFAULT_PRIORITY.endswith("activity_id")
    from massingplan.core.levelling import LevellingCandidate

    a = LevellingCandidate("A", 100, 0, 5)
    b = LevellingCandidate("B", 100, 0, 5)
    assert default_priority(a) < default_priority(b)


def test_the_result_is_json_safe(five_day) -> None:  # type: ignore[no-untyped-def]
    import json

    tasks, outcome, demands, availability = two_activities_one_crew(five_day)
    result = level(
        LevellingRequest(
            outcome=outcome,
            tasks=tasks,
            links=[],
            calendars={"5D": five_day},
            demands=demands,
            availability=availability,
        )
    )
    json.dumps(result.to_dict())


def test_the_objective_tuple_is_what_a_search_would_minimise(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, outcome, demands, availability = two_activities_one_crew(five_day)
    result = level(
        LevellingRequest(
            outcome=outcome,
            tasks=tasks,
            links=[],
            calendars={"5D": five_day},
            demands=demands,
            availability=availability,
            horizon=LevellingHorizon.EXTEND_FINISH,
        )
    )
    finish, peak, sum_squares = result.objective()
    assert finish == result.finish_after.toordinal()
    assert peak == 1.0
    assert sum_squares == 1.0


# -- determinism -----------------------------------------------------------

_DETERMINISM_PROBE = """
import sys
from datetime import date
sys.path.insert(0, %r)
from massingplan.core.levelling import (
    LevellingHorizon, LevellingRequest, level,
)
from massingplan.core.network import Task
from massingplan.core.resources import Demand, ResourceAvailability
from massingplan.core.schedule import schedule_network
from massingplan.core.timeaxis import WorkCalendar, WorkPattern

cal = WorkCalendar("5D", "Mon-Fri", WorkPattern(frozenset({0, 1, 2, 3, 4})))
cal.bind(date(2025, 1, 1), date(2030, 12, 31))
# Six deliberately tied activities: same duration, same float, same late start.
tasks = [Task(f"T{i}", "", 3, "5D") for i in range(6)]
outcome = schedule_network(tasks, [], {"5D": cal}, data_date=date(2026, 6, 1))
result = level(LevellingRequest(
    outcome=outcome, tasks=tasks, links=[], calendars={"5D": cal},
    demands=[Demand(t.id, "CREW", 1.0) for t in tasks],
    availability=[ResourceAvailability("CREW", 1.0)],
    horizon=LevellingHorizon.EXTEND_FINISH,
))
print("|".join(f"{m.activity_id}:{m.to_start}" for m in result.moves))
"""


def test_the_same_input_levels_identically_under_different_hash_seeds() -> None:
    """Six tied activities, run in fresh interpreters with different
    ``PYTHONHASHSEED``. An optimiser whose answer changes between runs cannot be
    reviewed, approved, or defended in a claim.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = _DETERMINISM_PROBE % repo_root
    outputs = set()
    for seed in ("0", "1", "524287"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        outputs.add(proc.stdout.strip())
    assert len(outputs) == 1, f"levelling differed between hash seeds: {outputs}"
    # And it actually did something, so the assertion above is not vacuous.
    assert outputs and next(iter(outputs))
