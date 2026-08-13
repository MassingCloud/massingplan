"""Resource profiles and deterministic levelling.

The textbook case is worked by hand in the first test; everything after it
defends a property the levelling result has to have to be usable as evidence.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from datetime import date

from massingplan.core.levelling import (
    DEFAULT_PRIORITY,
    LevellingCandidate,
    LevellingHorizon,
    LevellingMode,
    LevellingRequest,
    default_priority,
    level,
)
from massingplan.core.network import Link, RelationType, Task
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
from dataclasses import replace
from datetime import date
sys.path.insert(0, %r)
from massingplan.core.levelling import (
    LevellingCandidate,
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


# -- levelling and the logic it is levelling -------------------------------


def test_delaying_an_activity_delays_what_waits_on_it(five_day) -> None:  # type: ignore[no-untyped-def]
    """A and B both want the only crew on Monday; C follows B.

    A drives a ten-day chain, so it has no float and takes the crew first.
    B(2) is pushed off Mon 1 June to Wed 3 June. C is tied FS to B and the CPM
    started it Wed 3 June, when B still finished on the Tuesday.

    The precedence floor was C's *original* CPM start, so C stayed on Wed 3
    June -- the day B now starts, two days before B finishes. C waits on B and
    begins before it. Nothing in the result said so: the histogram is flat, the
    move list is short, and the schedule is impossible.
    """
    tasks = [
        Task("A", "Slab pour A", 2, "5D"),
        Task("D", "Cure and backprop", 10, "5D"),
        Task("B", "Slab pour B", 2, "5D"),
        Task("C", "Strike formwork", 1, "5D"),
    ]
    links = [Link("A", "D", RelationType.FS, 0), Link("B", "C", RelationType.FS, 0)]
    outcome = schedule_network(tasks, links, {"5D": five_day}, data_date=JUN1)
    assert outcome.dates["B"].start == date(2026, 6, 1)
    assert outcome.dates["C"].start == date(2026, 6, 3)

    result = level(
        LevellingRequest(
            outcome=outcome,
            tasks=tasks,
            links=links,
            calendars={"5D": five_day},
            demands=[Demand("A", "CREW", 1.0), Demand("B", "CREW", 1.0)],
            availability=[ResourceAvailability("CREW", units_per_day=1.0)],
            horizon=LevellingHorizon.EXTEND_FINISH,
            mode=LevellingMode.APPLIED,
        )
    )
    b_start, b_finish = result.spans["B"]
    c_start, _ = result.spans["C"]
    assert b_start == date(2026, 6, 3), "B should have been pushed off the shared crew"
    assert c_start > b_finish, f"C starts {c_start}, B finishes {b_finish}"
    assert c_start == date(2026, 6, 5)


def test_a_successor_is_placed_after_its_predecessor_not_merely_after_it_in_priority(
    five_day,  # type: ignore[no-untyped-def]
) -> None:
    """A zero-duration milestone ties for late start with the work behind it.

    An SS link ties predecessor and successor to the same late start, so the
    rule falls through to longest-first -- and P(2) is shorter than the S(5)
    that waits on it. The order comes out A, S, P: the successor is positioned
    against a predecessor that has no position yet, and an unplaced predecessor
    imposes no floor at all. Placing P afterwards then moves it past S.

    A positive-duration FS predecessor cannot show this; its late start is
    strictly earlier by its own duration, so it always sorts first. SS is where
    the sorted list and the precedence order come apart.
    """
    tasks = [
        Task("A", "Hoist run", 5, "5D"),  # sorts first, takes the crew
        Task("P", "Pour", 2, "5D"),
        Task("S", "Follow-on", 5, "5D"),
    ]
    links = [Link("P", "S", RelationType.SS, 0)]
    outcome = schedule_network(tasks, links, {"5D": five_day}, data_date=JUN1)

    ranked = sorted(
        (
            LevellingCandidate(
                t.id,
                outcome.network.late_start[t.id],
                outcome.network.total_float_days[t.id] or 0,
                t.duration_days,
            )
            for t in tasks
        ),
        key=default_priority,
    )
    assert [c.activity_id for c in ranked] == ["A", "S", "P"], "the premise of this test"

    result = level(
        LevellingRequest(
            outcome=outcome,
            tasks=tasks,
            links=links,
            calendars={"5D": five_day},
            demands=[Demand("A", "CREW", 1.0), Demand("P", "CREW", 1.0)],
            availability=[ResourceAvailability("CREW", units_per_day=1.0)],
            horizon=LevellingHorizon.EXTEND_FINISH,
            mode=LevellingMode.APPLIED,
        )
    )
    p_start, _ = result.spans["P"]
    s_start, _ = result.spans["S"]
    assert p_start == date(2026, 6, 8), "P should have been pushed off the shared crew"
    assert s_start >= p_start, f"SS broken: P starts {p_start}, S starts {s_start}"


def test_within_float_reports_the_finish_it_produced(five_day) -> None:  # type: ignore[no-untyped-def]
    """The horizon is kept by the algorithm, not by overwriting the answer.

    `finish_after` used to be reassigned to `finish_before` in this mode, so
    the field could not have disagreed however wrong the placement was. It is
    now the real maximum of the levelled spans, and it matches because every
    activity is bounded at its own late start.
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
            mode=LevellingMode.APPLIED,
        )
    )
    assert result.finish_after == max(span[1] for span in result.spans.values())
    assert result.finish_after == result.finish_before
    assert result.finish_moved_days == 0


def test_a_result_that_raises_a_peak_says_so(five_day) -> None:  # type: ignore[no-untyped-def]
    """First-fit can leave a resource worse than it found it. Say which."""
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
            mode=LevellingMode.APPLIED,
        )
    )
    assert result.raised_peaks == ()
    assert result.to_dict()["raised_peaks"] == []

    worse = replace(result, peak_after={"CREW": 9.0}, peak_before={"CREW": 2.0})
    assert worse.raised_peaks == ("CREW",)
