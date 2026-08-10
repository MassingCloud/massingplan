"""Baseline comparison and delay attribution.

The invariant under test throughout: **the contributions sum to the finish
move, exactly**. An analysis whose parts do not sum to the whole is an opinion
with numbers attached.
"""

from __future__ import annotations

import random
from datetime import date

from massingplan.core.compare import ChangeKind, DelayCause, LinkChange, MatchKey, compare
from massingplan.core.constraints import ConstraintType
from massingplan.core.network import Link, RelationType, Task
from massingplan.core.schedule import schedule_network

JUN1 = date(2026, 6, 1)


def run(tasks, links, five_day):  # type: ignore[no-untyped-def]
    return schedule_network(tasks, links, {"5D": five_day}, data_date=JUN1)


def five_chain(durations: list[int]) -> tuple[list[Task], list[Link]]:
    tasks = [Task(f"A{i}", f"Activity {i}", d, "5D") for i, d in enumerate(durations)]
    links = [Link(f"A{i}", f"A{i + 1}") for i in range(len(durations) - 1)]
    return tasks, links


def diff(base_t, base_l, curr_t, curr_l, five_day, **kw):  # type: ignore[no-untyped-def]
    return compare(
        run(base_t, base_l, five_day),
        run(curr_t, curr_l, five_day),
        baseline_network=(base_t, base_l),
        current_network=(curr_t, curr_l),
        **kw,
    )


# -- the invariant ---------------------------------------------------------


def test_a_duration_growth_is_attributed_to_the_activity_that_grew(five_day) -> None:  # type: ignore[no-untyped-def]
    """One activity grows by three days; the finish moves by three days; the
    attribution says so, and it sums.
    """
    base_t, base_l = five_chain([4, 4, 4, 4, 4])
    curr_t, curr_l = five_chain([4, 7, 4, 4, 4])
    result = diff(base_t, base_l, curr_t, curr_l, five_day)

    growth = [c for c in result.driving_path.attribution if c.cause is DelayCause.DURATION_GROWTH]
    assert len(growth) == 1
    assert growth[0].activity_id == "A1"
    assert growth[0].days == 3
    assert result.driving_path.attribution_sums


def test_the_attribution_sums_under_randomly_perturbed_networks(five_day) -> None:  # type: ignore[no-untyped-def]
    """A property test over the thing that has to be true for the report to be
    usable as evidence.
    """
    rng = random.Random(20260809)
    for _ in range(60):
        n = rng.randint(3, 7)
        base = [rng.randint(1, 12) for _ in range(n)]
        curr = [max(1, d + rng.randint(-4, 6)) for d in base]
        base_t, base_l = five_chain(base)
        curr_t, curr_l = five_chain(curr)
        result = diff(base_t, base_l, curr_t, curr_l, five_day)
        total = sum(c.days for c in result.driving_path.attribution)
        assert total == result.driving_path.finish_move_days, (
            f"base={base} curr={curr}: attribution summed to {total}, "
            f"finish moved {result.driving_path.finish_move_days}"
        )


def test_a_residual_is_named_rather_than_dropped(five_day) -> None:  # type: ignore[no-untyped-def]
    """Where the change is not on the driving path, the movement still has to be
    accounted for -- as PATH_SWITCH or UNEXPLAINED, never by rounding it away.
    """
    base_t = [Task("A", "", 10, "5D"), Task("B", "", 2, "5D"), Task("END", "", 1, "5D")]
    base_l = [Link("A", "END"), Link("B", "END")]
    curr_t = [Task("A", "", 3, "5D"), Task("B", "", 20, "5D"), Task("END", "", 1, "5D")]
    curr_l = list(base_l)
    result = diff(base_t, base_l, curr_t, curr_l, five_day)

    assert result.driving_path.attribution_sums
    causes = {c.cause for c in result.driving_path.attribution}
    assert DelayCause.PATH_SWITCH in causes or DelayCause.UNEXPLAINED in causes


def test_a_path_switch_names_both_paths(five_day) -> None:  # type: ignore[no-untyped-def]
    base_t = [Task("LEFT", "", 10, "5D"), Task("RIGHT", "", 2, "5D"), Task("END", "", 1, "5D")]
    base_l = [Link("LEFT", "END"), Link("RIGHT", "END")]
    curr_t = [Task("LEFT", "", 2, "5D"), Task("RIGHT", "", 15, "5D"), Task("END", "", 1, "5D")]
    result = diff(base_t, base_l, curr_t, list(base_l), five_day)

    assert "RIGHT" in result.driving_path.entered
    assert "LEFT" in result.driving_path.left
    switch = next(c for c in result.driving_path.attribution if c.cause is DelayCause.PATH_SWITCH)
    assert "LEFT" in switch.evidence
    assert "RIGHT" in switch.evidence


# -- identity matching -----------------------------------------------------


def test_matching_on_code_pairs_a_rebaseline_that_id_matching_cannot(five_day) -> None:  # type: ignore[no-untyped-def]
    """A re-baseline exported from P6 has entirely new task ids.

    Matching on id reports every activity as removed-and-added -- technically
    correct, completely useless. Matching on the planner's own code works.
    """
    base_t, base_l = five_chain([4, 4, 4])
    curr_t = [Task(f"NEW{i}", f"Activity {i}", 4, "5D") for i in range(3)]
    curr_l = [Link("NEW0", "NEW1"), Link("NEW1", "NEW2")]
    codes_base = {f"A{i}": f"C{i}" for i in range(3)}
    codes_curr = {f"NEW{i}": f"C{i}" for i in range(3)}

    by_id = diff(base_t, base_l, curr_t, curr_l, five_day, match=MatchKey.ID)
    added = sum(1 for a in by_id.activities if ChangeKind.ADDED in a.kinds)
    removed = sum(1 for a in by_id.activities if ChangeKind.REMOVED in a.kinds)
    assert added == 3 and removed == 3

    by_code = diff(
        base_t,
        base_l,
        curr_t,
        curr_l,
        five_day,
        match=MatchKey.CODE,
        baseline_codes=codes_base,
        current_codes=codes_curr,
    )
    assert all(ChangeKind.ADDED not in a.kinds for a in by_code.activities)
    assert all(a.matched_by is MatchKey.CODE for a in by_code.activities)


def test_two_same_named_activities_are_reported_ambiguous_rather_than_paired(
    five_day,
) -> None:  # type: ignore[no-untyped-def]
    """Pairing "Pour slab" on level 2 with "Pour slab" on level 5 reports a
    forty-day slip that never happened. That gets a claim thrown out.
    """
    base_t = [Task("A", "Pour slab", 4, "5D"), Task("B", "Pour slab", 4, "5D")]
    curr_t = [Task("A", "Pour slab", 4, "5D"), Task("B", "Pour slab", 9, "5D")]
    result = diff(base_t, [], curr_t, [], five_day, match=MatchKey.NAME_AND_WBS)
    assert result.ambiguous_matches
    # And nothing was paired on that identity, so no false variance is reported.
    assert all(a.matched_by is None for a in result.activities)


# -- change kinds ----------------------------------------------------------


def test_relogicking_is_detected(five_day) -> None:  # type: ignore[no-untyped-def]
    base_t, base_l = five_chain([4, 4, 4])
    curr_l = [Link("A0", "A1"), Link("A1", "A2", RelationType.SS)]
    result = diff(base_t, base_l, base_t, curr_l, five_day)
    a2 = next(a for a in result.activities if a.activity_id == "A2")
    assert ChangeKind.RELOGICKED in a2.kinds
    assert any(link.change is LinkChange.TYPE_CHANGED for link in result.links)


def test_an_added_and_a_removed_link_are_both_reported(five_day) -> None:  # type: ignore[no-untyped-def]
    base_t, _ = five_chain([4, 4, 4])
    base_l = [Link("A0", "A1")]
    curr_l = [Link("A1", "A2")]
    result = diff(base_t, base_l, base_t, curr_l, five_day)
    changes = {(link.predecessor, link.successor): link.change for link in result.links}
    assert changes[("A0", "A1")] is LinkChange.REMOVED
    assert changes[("A1", "A2")] is LinkChange.ADDED


def test_a_lag_change_is_reported_and_attributed(five_day) -> None:  # type: ignore[no-untyped-def]
    base_t, _ = five_chain([4, 4])
    base_l = [Link("A0", "A1", RelationType.FS, 0)]
    curr_l = [Link("A0", "A1", RelationType.FS, 5)]
    result = diff(base_t, base_l, base_t, curr_l, five_day)
    assert any(link.change is LinkChange.LAG_CHANGED for link in result.links)
    lag = next(c for c in result.driving_path.attribution if c.cause is DelayCause.LAG_GROWTH)
    assert lag.days == 5
    assert result.driving_path.attribution_sums


def test_a_constraint_change_is_reported(five_day) -> None:  # type: ignore[no-untyped-def]
    base_t, base_l = five_chain([4, 4])
    curr_t = [
        base_t[0],
        Task(
            "A1",
            "Activity 1",
            4,
            "5D",
            constraint=ConstraintType.START_ON_OR_AFTER,
            constraint_date=date(2026, 7, 1),
        ),
    ]
    result = diff(base_t, base_l, curr_t, base_l, five_day)
    a1 = next(a for a in result.activities if a.activity_id == "A1")
    assert ChangeKind.CONSTRAINT_CHANGED in a1.kinds


def test_criticality_changes_are_tracked_both_ways(five_day) -> None:  # type: ignore[no-untyped-def]
    base_t = [Task("L", "", 10, "5D"), Task("R", "", 2, "5D"), Task("E", "", 1, "5D")]
    links = [Link("L", "E"), Link("R", "E")]
    curr_t = [Task("L", "", 2, "5D"), Task("R", "", 10, "5D"), Task("E", "", 1, "5D")]
    result = diff(base_t, links, curr_t, links, five_day)
    assert "R" in result.criticality_gained
    assert "L" in result.criticality_lost


def test_progress_is_reported_as_a_change(five_day) -> None:  # type: ignore[no-untyped-def]
    base_t, base_l = five_chain([4, 4])
    curr_t = [
        Task(
            "A0",
            "Activity 0",
            4,
            "5D",
            actual_start=date(2026, 6, 1),
            actual_finish=date(2026, 6, 4),
        ),
        base_t[1],
    ]
    result = diff(base_t, base_l, curr_t, base_l, five_day)
    a0 = next(a for a in result.activities if a.activity_id == "A0")
    assert ChangeKind.PROGRESS_ADDED in a0.kinds


def test_an_unchanged_schedule_reports_no_changes(five_day) -> None:  # type: ignore[no-untyped-def]
    base_t, base_l = five_chain([4, 4, 4])
    result = diff(base_t, base_l, base_t, base_l, five_day)
    assert result.changed() == []
    assert result.driving_path.finish_move_days == 0
    assert result.driving_path.attribution_sums


# -- levelling attribution -------------------------------------------------


def test_a_levelling_move_is_blamed_on_the_leveller_not_the_subcontractor(
    five_day,
) -> None:  # type: ignore[no-untyped-def]
    """Without passing the moves in, a smoothed schedule reads as if the trades
    slipped. That is a report that blames the wrong party.
    """
    from massingplan.core.levelling import Move

    base_t, base_l = five_chain([4, 4, 4])
    curr_t, curr_l = five_chain([4, 4, 4])
    moves = [Move("A1", date(2026, 6, 5), date(2026, 6, 10), 3, ("CREW",), 2)]
    result = compare(
        run(base_t, base_l, five_day),
        run(curr_t, curr_l, five_day),
        baseline_network=(base_t, base_l),
        current_network=(curr_t, curr_l),
        levelling_moves=moves,
    )
    causes = {c.cause for c in result.driving_path.attribution}
    assert DelayCause.LEVELLING in causes


# -- transport -------------------------------------------------------------


def test_the_comparison_is_json_safe(five_day) -> None:  # type: ignore[no-untyped-def]
    import json

    base_t, base_l = five_chain([4, 4, 4])
    curr_t, curr_l = five_chain([4, 9, 4])
    json.dumps(diff(base_t, base_l, curr_t, curr_l, five_day).to_dict())


def test_the_summary_reports_the_finish_move_and_the_change_counts(five_day) -> None:  # type: ignore[no-untyped-def]
    base_t, base_l = five_chain([4, 4, 4])
    curr_t, curr_l = five_chain([4, 9, 4])
    summary = diff(base_t, base_l, curr_t, curr_l, five_day).summary()
    assert summary["finish_move_days"] == 7  # 5 working days across a weekend
    assert summary["changed_count"] >= 1
    assert summary["driving_path"]["attribution_sums"] is True
