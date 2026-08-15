"""Modelled delay analysis, worked by hand.

The chain every test starts from, on a Mon-Fri calendar:

    A  10 days   Mon 1 Jun - Fri 12 Jun
    B  10 days   Mon 15 Jun - Fri 26 Jun     finish Fri 26 June

The concurrency test is the one that matters. Two five-day delays running at
the same time move the finish five days, not ten, and an analysis that reports
the sum is making a case rather than measuring.

**Every impact here is in working days.** Calendar days do not add up: two
delays of two and one working days in series move the finish three working
days, which can span a weekend and read as five calendar days -- against a
"sum" of three. That produced negative concurrency, a number that cannot
exist, on 14 of 150 random networks before the arithmetic was moved onto the
right axis.
"""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.modelled import (
    DelayEvent,
    ModelledDelayError,
    collapsed_as_built,
    impacted_as_planned,
)
from massingplan.core.network import Link, RelationType, Task
from massingplan.core.schedule import schedule_network

JUN1 = date(2026, 6, 1)


def chain(five_day):  # type: ignore[no-untyped-def]
    tasks = [Task("A", "Substructure", 10, "5D"), Task("B", "Frame", 10, "5D")]
    links = [Link("A", "B", RelationType.FS, 0)]
    return tasks, links, {"5D": five_day}


# -- impacted as-planned (MIP 3.6) -----------------------------------------


def test_an_inserted_delay_pushes_the_finish_and_names_its_method(five_day) -> None:  # type: ignore[no-untyped-def]
    """A five-day event in front of A moves everything behind it."""
    tasks, links, cals = chain(five_day)
    result = impacted_as_planned(
        tasks,
        links,
        cals,
        events=[DelayEvent("E1", "Late possession", 5, impacts="A")],
        data_date=JUN1,
    )
    assert "3.6" in result.mip
    assert result.method == "impacted as-planned"
    assert result.unimpacted_finish == date(2026, 6, 26)
    assert result.total_days == 5, "five working days, counted on the project calendar"
    assert result.per_event[0].days == 5
    assert result.per_event[0].calendar_days == 7, "the same move as elapsed time"


def test_an_event_absorbed_by_float_reports_zero_and_says_why(five_day) -> None:  # type: ignore[no-untyped-def]
    """Not every delay is a delay. A modelled event off the driving path moves
    nothing, and reporting that as zero is the finding."""
    tasks = [
        Task("A", "Substructure", 10, "5D"),
        Task("B", "Frame", 10, "5D"),
        Task("C", "Signage", 1, "5D"),  # parallel, enormous float
    ]
    links = [Link("A", "B", RelationType.FS, 0)]
    result = impacted_as_planned(
        tasks,
        links,
        {"5D": five_day},
        events=[DelayEvent("E1", "Sign delivery", 3, impacts="C")],
        data_date=JUN1,
    )
    assert result.total_days == 0
    assert result.per_event[0].days == 0
    assert any("absorbed by float" in note for note in result.notes)


def test_two_concurrent_delays_do_not_add_up(five_day) -> None:  # type: ignore[no-untyped-def]
    """The single most argued number in delay disputes.

    Both events hit A, both five days, both starting from the same point. Each
    on its own moves the finish seven calendar days. Together they still move
    it seven, because they ran at the same time and only one was ever driving.
    An analysis reporting fourteen has double-counted a delay nobody suffered
    twice.
    """
    tasks, links, cals = chain(five_day)
    result = impacted_as_planned(
        tasks,
        links,
        cals,
        events=[
            DelayEvent("E1", "Late possession", 5, impacts="A"),
            DelayEvent("E2", "Late design", 5, impacts="A"),
        ],
        data_date=JUN1,
    )
    assert result.per_event[0].days == 5
    assert result.per_event[1].days == 5
    assert result.sum_of_individual_days == 10
    assert result.total_days == 5, "they ran concurrently"
    assert result.concurrency_days == 5
    assert result.is_concurrent is True


def test_two_sequential_delays_do_add_up(five_day) -> None:  # type: ignore[no-untyped-def]
    """The control for the concurrency test, and the reason it exists.

    One event in front of A and one in front of B are on the same path but not
    at the same time, so the finish moves by both and `concurrency_days` is
    zero.

    Without this control the module passed the concurrency test for the wrong
    reason. Events used to hang off the impacted activity with no predecessors
    of their own, so every one of them floated back to the data date and *every
    pair came out concurrent* -- four different constructions all reported the
    same seven days. An event now inherits the logic feeding the activity it
    delays, so a delay early in the chain pushes a later one and the two add up
    as they did on site.
    """
    tasks, links, cals = chain(five_day)
    result = impacted_as_planned(
        tasks,
        links,
        cals,
        events=[
            DelayEvent("E1", "Late possession", 5, impacts="A"),
            DelayEvent("E2", "Late steel", 5, impacts="B"),
        ],
        data_date=JUN1,
    )
    assert result.total_days == result.sum_of_individual_days == 10
    assert result.concurrency_days == 0
    assert result.is_concurrent is False


def test_an_onset_holds_an_event_where_it_actually_happened(five_day) -> None:  # type: ignore[no-untyped-def]
    """An inserted event runs as soon as the logic allows unless it is pinned.

    Without an onset the delay to B follows A immediately and costs the five
    working days it lasts. With an onset a week later it could not have started
    then, so the gap between the logic and the event is lost as well -- which
    is the difference between "this took five days" and "this took five days,
    a week after the work was ready for it".
    """
    tasks, links, cals = chain(five_day)
    free = impacted_as_planned(
        tasks, links, cals, events=[DelayEvent("E", "Late steel", 5, impacts="B")], data_date=JUN1
    )
    pinned = impacted_as_planned(
        tasks,
        links,
        cals,
        events=[DelayEvent("E", "Late steel", 5, impacts="B", onset=date(2026, 6, 22))],
        data_date=JUN1,
    )
    assert free.total_days == 5, "the five working days it lasted"
    assert pinned.total_days == 10, "plus the week it waited to begin"


# -- collapsed as-built (MIP 3.9) ------------------------------------------


def test_removing_a_delay_from_the_as_built_says_when_it_would_have_finished(five_day) -> None:  # type: ignore[no-untyped-def]
    """The but-for programme, and the mirror of the additive method."""
    as_built = [
        Task("A", "Substructure", 10, "5D"),
        Task("DELAY::E1", "Late possession", 5, "5D"),
        Task("B", "Frame", 10, "5D"),
    ]
    links = [
        Link("DELAY::E1", "A", RelationType.FS, 0),
        Link("A", "B", RelationType.FS, 0),
    ]
    result = collapsed_as_built(
        as_built,
        links,
        {"5D": five_day},
        events=[DelayEvent("E1", "Late possession", 5, impacts="A")],
        data_date=JUN1,
    )
    assert "3.9" in result.mip
    assert result.method == "collapsed as-built"
    assert result.impacted_finish > result.unimpacted_finish
    assert result.total_days == 5


def test_the_collapse_strips_actuals_or_it_can_never_move(five_day) -> None:  # type: ignore[no-untyped-def]
    """An as-built activity pinned to the date it really happened cannot move.

    Leave the actuals in and the collapsed network reports no change however
    much is removed -- a nil result that reads as a finding.
    """
    as_built = [
        Task(
            "A",
            "Substructure",
            10,
            "5D",
            actual_start=date(2026, 6, 8),
            actual_finish=date(2026, 6, 19),
            remaining_days=0,
        ),
        Task("DELAY::E1", "Late possession", 5, "5D"),
        Task("B", "Frame", 10, "5D"),
    ]
    links = [
        Link("DELAY::E1", "A", RelationType.FS, 0),
        Link("A", "B", RelationType.FS, 0),
    ]
    result = collapsed_as_built(
        as_built,
        links,
        {"5D": five_day},
        events=[DelayEvent("E1", "Late possession", 5, impacts="A")],
        data_date=JUN1,
    )
    assert result.total_days > 0, "the collapse moved nothing, so the actuals were still pinning it"
    assert any("stripped" in note for note in result.notes)


def test_a_link_to_a_removed_event_goes_with_it(five_day) -> None:  # type: ignore[no-untyped-def]
    """A relationship pointing at an activity that is no longer there would
    make the collapsed network unschedulable."""
    as_built = [
        Task("A", "Substructure", 10, "5D"),
        Task("DELAY::E1", "Late possession", 5, "5D"),
    ]
    links = [Link("DELAY::E1", "A", RelationType.FS, 0)]
    result = collapsed_as_built(
        as_built,
        links,
        {"5D": five_day},
        events=[DelayEvent("E1", "Late possession", 5, impacts="A")],
        data_date=JUN1,
    )
    assert result.outcome is not None
    assert set(result.outcome.dates) == {"A"}


# -- what they refuse ------------------------------------------------------


def test_an_event_attached_to_nothing_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    """It would be reported as a zero-day impact rather than as the mistake."""
    tasks, links, cals = chain(five_day)
    with pytest.raises(ModelledDelayError, match="not in this network"):
        impacted_as_planned(
            tasks, links, cals, events=[DelayEvent("E", "Ghost", 5, impacts="NOPE")], data_date=JUN1
        )


def test_modelling_no_events_is_a_missing_input_not_an_empty_result(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links, cals = chain(five_day)
    with pytest.raises(ModelledDelayError, match="models nothing"):
        impacted_as_planned(tasks, links, cals, events=[], data_date=JUN1)
    with pytest.raises(ModelledDelayError, match="removes nothing"):
        collapsed_as_built(tasks, links, cals, events=[], data_date=JUN1)


def test_the_same_event_twice_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    tasks, links, cals = chain(five_day)
    with pytest.raises(ModelledDelayError, match="appears twice"):
        impacted_as_planned(
            tasks,
            links,
            cals,
            events=[DelayEvent("E", "A", 5, impacts="A"), DelayEvent("E", "B", 5, impacts="A")],
            data_date=JUN1,
        )


def test_a_negative_delay_is_refused() -> None:
    with pytest.raises(ModelledDelayError, match="negative duration"):
        DelayEvent("E", "Time machine", -5, impacts="A")


def test_collapsing_an_event_that_is_not_in_the_network_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    """Subtractive removes what is there; additive adds what is not. Silently
    doing nothing would report a nil but-for result."""
    tasks, links, cals = chain(five_day)
    with pytest.raises(ModelledDelayError, match="not in the as-built network"):
        collapsed_as_built(
            tasks, links, cals, events=[DelayEvent("E1", "Ghost", 5, impacts="A")], data_date=JUN1
        )


# -- the boundary with the observational method ----------------------------


def test_a_modelled_result_cannot_come_out_of_the_observational_entry_point() -> None:
    """The roadmap's acceptance criterion, asserted structurally.

    `windows.analyse` returns a `WindowsAnalysis` and cannot return a
    `ModelledResult` -- they are different types from different modules, and
    neither imports the other. That is what keeps a report from presenting a
    counterfactual as an observation.
    """
    import massingplan.core.modelled as modelled_mod
    import massingplan.core.windows as windows_mod

    assert "modelled" not in windows_mod.__dict__
    source = (windows_mod.__doc__ or "") + str(windows_mod.__all__)
    assert "ModelledResult" not in source
    assert windows_mod.WindowsAnalysis is not modelled_mod.ModelledResult
    assert "MIP 3.3" in windows_mod.WindowsAnalysis.method
    assert "3.6" in modelled_mod.impacted_as_planned.__doc__
    assert "3.9" in modelled_mod.collapsed_as_built.__doc__


def test_the_result_is_json_safe_and_carries_its_method(five_day) -> None:  # type: ignore[no-untyped-def]
    import json

    tasks, links, cals = chain(five_day)
    result = impacted_as_planned(
        tasks,
        links,
        cals,
        events=[DelayEvent("E1", "Late possession", 5, impacts="A", responsibility="employer")],
        data_date=JUN1,
    )
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["mip"].startswith("AACE 29R-03 MIP 3.6")
    assert payload["per_event"][0]["responsibility"] == "employer"
    assert payload["concurrency_days"] == 0


def test_the_unimpacted_finish_is_the_schedule_the_caller_passed_in(five_day) -> None:  # type: ignore[no-untyped-def]
    """The baseline is not recomputed with anything added, or the delta is
    measured against a network the caller never saw."""
    tasks, links, cals = chain(five_day)
    plain = schedule_network(tasks, links, cals, data_date=JUN1)
    result = impacted_as_planned(
        tasks, links, cals, events=[DelayEvent("E", "X", 5, impacts="A")], data_date=JUN1
    )
    assert result.unimpacted_finish == plain.project_finish


def test_concurrency_is_exact_only_on_one_calendar(five_day, six_day) -> None:  # type: ignore[no-untyped-def]
    """A mixed-calendar network cannot make delay additive, and says so.

    "How many days did the finish move" is only well posed against *a*
    calendar, and a project finish is one date. Measured over 400 random
    two-calendar networks the discrepancy reached -4 days -- so this is not a
    tolerance to wave away, and a report quoting `concurrency_days` as a
    precise entitlement on a mixed programme is quoting something the
    arithmetic does not support.
    """
    single = impacted_as_planned(
        [Task("A", "A", 10, "5D")],
        [],
        {"5D": five_day},
        events=[DelayEvent("E", "x", 3, impacts="A")],
        data_date=JUN1,
    )
    assert single.is_exact is True
    assert single.calendar_count == 1
    assert single.to_dict()["concurrency_is_exact"] is True

    mixed = impacted_as_planned(
        [Task("A", "A", 10, "5D"), Task("B", "B", 5, "6D")],
        [Link("A", "B", RelationType.FS, 0)],
        {"5D": five_day, "6D": six_day},
        events=[DelayEvent("E", "x", 3, impacts="A")],
        data_date=JUN1,
    )
    assert mixed.is_exact is False
    assert mixed.calendar_count == 2
    assert mixed.basis_calendar_id in ("5D", "6D")
    assert any("calendars" in note for note in mixed.notes), (
        "a mixed-calendar network must name the basis its day-counts were taken on"
    )
