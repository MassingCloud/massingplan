"""Schedule compression, worked by hand.

The chain, on a Mon-Fri calendar:

    A  10 days  £100/day to crash, up to 4 days
    B  10 days  £500/day to crash, up to 4 days
    C  10 days  not crashable

Cheapest useful day first, so A is bought before B, and nothing is bought on an
activity that has stopped driving.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date

import pytest

from massingplan.core.compression import (
    CompressionError,
    CrashCost,
    CrashOption,
    FastTrackOption,
    apply,
    plan,
)
from massingplan.core.network import Link, RelationType, Task
from massingplan.core.schedule import schedule_network

JUN1 = date(2026, 6, 1)


def chain(five_day):  # type: ignore[no-untyped-def]
    tasks = [
        Task("A", "Substructure", 10, "5D"),
        Task("B", "Frame", 10, "5D"),
        Task("C", "Envelope", 10, "5D"),
    ]
    links = [
        Link("A", "B", RelationType.FS, 0),
        Link("B", "C", RelationType.FS, 0),
    ]
    return tasks, links, {"5D": five_day}


COSTS = [
    CrashCost("A", cost_per_day=100.0, max_days=4),
    CrashCost("B", cost_per_day=500.0, max_days=4),
]


# -- crashing --------------------------------------------------------------


def test_the_cheapest_useful_day_is_bought_first(five_day) -> None:  # type: ignore[no-untyped-def]
    """A at £100 before B at £500, and only as far as A can go."""
    tasks, links, cals = chain(five_day)
    result = plan(tasks, links, cals, target_days=2, costs=COSTS, data_date=JUN1)

    assert result.meets_target
    assert all(isinstance(o, CrashOption) and o.activity_id == "A" for o in result.options)
    assert result.total_cost == 200.0
    assert result.days_available == 2


def test_it_moves_on_to_the_dearer_activity_when_the_cheap_one_runs_out(five_day) -> None:  # type: ignore[no-untyped-def]
    """A gives four days at £100; the fifth has to come from B at £500."""
    tasks, links, cals = chain(five_day)
    result = plan(tasks, links, cals, target_days=5, costs=COSTS, data_date=JUN1)

    bought = [(o.activity_id, o.cost) for o in result.options]  # type: ignore[attr-defined]
    assert [a for a, _ in bought] == ["A", "A", "A", "A", "B"]
    assert result.total_cost == 4 * 100.0 + 500.0
    assert result.meets_target


def test_nothing_is_bought_on_an_activity_that_is_not_driving(five_day) -> None:  # type: ignore[no-untyped-def]
    """A day that buys nothing is not for sale.

    D runs in parallel with the whole chain and has weeks of float. Crashing it
    is cheap and worthless, and a greedy algorithm sorting on price alone would
    buy every day of it before touching anything that mattered.
    """
    tasks, links, cals = chain(five_day)
    tasks = [*tasks, Task("D", "Signage", 2, "5D")]
    result = plan(
        tasks,
        links,
        cals,
        target_days=2,
        costs=[CrashCost("D", cost_per_day=1.0, max_days=2), *COSTS],
        data_date=JUN1,
    )
    assert all(o.activity_id != "D" for o in result.options)  # type: ignore[attr-defined]
    assert result.total_cost == 200.0


def test_a_target_that_cannot_be_met_returns_what_it_found(five_day) -> None:  # type: ignore[no-untyped-def]
    """Eight of the ten days asked for is the answer to the question.

    Raising would leave the caller to find the eight by bisection.
    """
    tasks, links, cals = chain(five_day)
    result = plan(tasks, links, cals, target_days=30, costs=COSTS, data_date=JUN1)

    assert not result.meets_target
    assert result.days_available > 0
    assert any("ran out of compression" in note for note in result.notes)


def test_days_saved_is_the_project_move_not_the_days_taken_off(five_day) -> None:  # type: ignore[no-untyped-def]
    """Shortening a driving activity past the point where another path takes
    over buys nothing further, and counting it is how a crash plan overruns."""
    tasks = [Task("A", "Substructure", 10, "5D"), Task("P", "Parallel", 8, "5D")]
    links: list[Link] = []
    result = plan(
        tasks,
        links,
        {"5D": five_day},
        target_days=10,
        costs=[CrashCost("A", cost_per_day=100.0, max_days=8)],
        data_date=JUN1,
    )
    # A can only be shortened until P becomes the longest path.
    assert result.days_available <= 4
    assert not result.meets_target


# -- fast-tracking ---------------------------------------------------------


def test_an_overlap_costs_no_money_and_states_its_risk(five_day) -> None:  # type: ignore[no-untyped-def]
    """The price of fast-tracking is rework, not pounds -- and the engine
    cannot compute what that is worth, so it does not pretend to."""
    tasks, links, cals = chain(five_day)
    result = plan(tasks, links, cals, target_days=3, fast_trackable=[("A", "B")], data_date=JUN1)
    assert result.options
    option = result.options[0]
    assert isinstance(option, FastTrackOption)
    assert option.to_dict()["cost"] == 0.0
    assert "information" in option.risk
    assert "rework" in option.risk.lower()


def test_free_days_are_taken_before_paid_ones(five_day) -> None:  # type: ignore[no-untyped-def]
    """An overlap that costs nothing beats a crash that costs anything."""
    tasks, links, cals = chain(five_day)
    result = plan(
        tasks,
        links,
        cals,
        target_days=2,
        costs=COSTS,
        fast_trackable=[("A", "B")],
        data_date=JUN1,
    )
    assert all(isinstance(o, FastTrackOption) for o in result.options)
    assert result.total_cost == 0.0


def test_overlapping_a_pair_that_is_not_linked_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    """That is not compression, it is a change to the logic."""
    tasks, links, cals = chain(five_day)
    with pytest.raises(CompressionError, match="no link between them"):
        plan(tasks, links, cals, target_days=2, fast_trackable=[("A", "C")], data_date=JUN1)


def test_only_a_finish_start_pair_can_be_overlapped(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, _links, cals = chain(five_day)
    links = [Link("A", "B", RelationType.SS, 0), Link("B", "C", RelationType.FS, 0)]
    with pytest.raises(CompressionError, match="not Finish-Start"):
        plan(tasks, links, cals, target_days=2, fast_trackable=[("A", "B")], data_date=JUN1)


# -- what it refuses -------------------------------------------------------


def test_a_negative_cost_per_day_is_refused() -> None:
    """It would mean the schedule pays you to shorten it."""
    with pytest.raises(CompressionError, match="pays you"):
        CrashCost("A", cost_per_day=-10.0, max_days=2)


def test_a_cost_for_an_activity_that_is_not_here_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links, cals = chain(five_day)
    with pytest.raises(CompressionError, match="not in this network"):
        plan(
            tasks,
            links,
            cals,
            target_days=1,
            costs=[CrashCost("NOPE", cost_per_day=1.0, max_days=1)],
            data_date=JUN1,
        )


def test_a_negative_target_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links, cals = chain(five_day)
    with pytest.raises(CompressionError, match="finish later"):
        plan(tasks, links, cals, target_days=-3, costs=COSTS, data_date=JUN1)


# -- nothing is applied without being asked --------------------------------


def test_planning_changes_nothing(five_day) -> None:  # type: ignore[no-untyped-def]
    """Compression costs money and creates risk. A module that returned a
    compressed schedule from the call that evaluated it would be making a
    commercial decision on somebody's behalf."""
    tasks, links, cals = chain(five_day)
    before = schedule_network(tasks, links, cals, data_date=JUN1).to_rows()
    plan(
        tasks, links, cals, target_days=4, costs=COSTS, fast_trackable=[("A", "B")], data_date=JUN1
    )
    after = schedule_network(tasks, links, cals, data_date=JUN1).to_rows()
    assert before == after
    assert [t.duration_days for t in tasks] == [10, 10, 10]


def test_applying_the_chosen_options_produces_the_finish_the_plan_promised(five_day) -> None:  # type: ignore[no-untyped-def]
    """The number in the plan has to be the number you get, or the plan is a
    sales document."""
    tasks, links, cals = chain(five_day)
    result = plan(tasks, links, cals, target_days=3, costs=COSTS, data_date=JUN1)

    new_tasks, new_links = apply(tasks, links, result.options)
    outcome = schedule_network(new_tasks, new_links, cals, data_date=JUN1)
    assert outcome.project_finish == result.best_finish


def test_applying_a_fast_track_produces_the_promised_finish_too(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links, cals = chain(five_day)
    result = plan(
        tasks, links, cals, target_days=3, fast_trackable=[("A", "B"), ("B", "C")], data_date=JUN1
    )
    new_tasks, new_links = apply(tasks, links, result.options)
    outcome = schedule_network(new_tasks, new_links, cals, data_date=JUN1)
    assert outcome.project_finish == result.best_finish


# -- determinism -----------------------------------------------------------


def test_the_plan_is_identical_under_a_changed_hash_seed() -> None:
    """A compression plan that differs each time cannot be taken to a client."""
    script = (
        "from datetime import date;"
        "from massingplan.core.compression import CrashCost, plan;"
        "from massingplan.core.network import Link, RelationType, Task;"
        "from massingplan.core.timeaxis import WorkCalendar, WorkPattern;"
        "c=WorkCalendar('5D', WorkPattern(frozenset({0,1,2,3,4})));"
        "t=[Task(i,i,10,'5D') for i in 'ABC'];"
        "l=[Link('A','B',RelationType.FS,0),Link('B','C',RelationType.FS,0)];"
        "r=plan(t,l,{'5D':c},target_days=6,"
        "costs=[CrashCost('A',100.0,4),CrashCost('B',100.0,4),CrashCost('C',100.0,4)],"
        "fast_trackable=[('A','B')],data_date=date(2026,6,1));"
        "print([o.to_dict() for o in r.options])"
    )
    answers = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True
        )
        answers.add(out.stdout.strip())
    assert len(answers) == 1, f"the plan differed between hash seeds: {answers}"


def test_crashing_and_overlapping_the_same_activity_still_overlaps(five_day) -> None:  # type: ignore[no-untyped-def]
    """The lag has to come from the duration the activity ends up with.

    `apply` built its lookup from the *original* tasks, so an activity that was
    both shortened and overlapped had its lag computed against a duration it no
    longer had. Crash A from ten days to four and overlap A->B by two, and the
    link came out `SS(8)`: B started five working days *after* A finished. The
    caller asked for an overlap and got a gap, on a schedule that computes
    cleanly and looks entirely valid.
    """
    tasks = [Task("A", "Substructure", 10, "5D"), Task("B", "Frame", 10, "5D")]
    links = [Link("A", "B", RelationType.FS, 0)]
    stamp = date(2026, 7, 10)

    new_tasks, new_links = apply(
        tasks,
        links,
        [
            CrashOption("A", days=6, cost=600.0, finish_before=stamp, finish_after=stamp),
            FastTrackOption(
                "A", "B", overlap_days=2, finish_before=stamp, finish_after=stamp, risk="x"
            ),
        ],
    )
    shortened = next(t for t in new_tasks if t.id == "A")
    assert shortened.duration_days == 4
    assert new_links[0].lag_days == 2, "4-day activity, 2-day overlap: the lag is 2, not 10-2"

    rows = {
        r["activity_id"]: r
        for r in schedule_network(new_tasks, new_links, {"5D": five_day}, data_date=JUN1).to_rows()
    }
    assert rows["B"]["start"] <= rows["A"]["finish"], (
        f"B starts {rows['B']['start']} and A finishes {rows['A']['finish']}: that is a gap, "
        "not an overlap"
    )
