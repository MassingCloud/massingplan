"""Contemporaneous windows analysis, worked by hand.

The invariant throughout: **the windows sum to the whole**. A project that
finished eighty days late did not do it in one step, and an analysis whose
periods do not add up to the overall movement is an opinion with numbers
attached.

Every expected number here is written out. A windows analysis is arithmetic a
planner has to be able to check against their own updates, because the output
ends up in front of a tribunal.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from massingplan.core.compare import MatchKey
from massingplan.core.network import Link, RelationType, Task
from massingplan.core.schedule import schedule_network
from massingplan.core.windows import Update, WindowsError, analyse

JUN1 = date(2026, 6, 1)


#: The as-planned chain every test starts from, on a Mon-Fri calendar:
#:
#:     A0  Mon 1 Jun - Fri 5 Jun
#:     A1  Mon 8 Jun - Fri 12 Jun
#:     A2  Mon 15 Jun - Fri 19 Jun      finish Fri 19 June
#:
#: Written out because every expected number below is read off it.
PLANNED = [
    (date(2026, 6, 1), date(2026, 6, 5)),
    (date(2026, 6, 8), date(2026, 6, 12)),
    (date(2026, 6, 15), date(2026, 6, 19)),
]


#: A1 ran 8 days rather than its planned 5, so it actually finished Wed 17 June.
A1_ACTUAL = (date(2026, 6, 8), date(2026, 6, 17))


def update(  # type: ignore[no-untyped-def]
    day: date,
    durations: list[int],
    five_day,
    complete: int = 0,
    actuals: dict[int, tuple[date, date]] | None = None,
    **kw,
):
    """One contemporaneous update: the chain as it stood on `day`.

    `complete` is how many leading activities had finished by then, carrying
    the actual dates they were planned for -- so work that got done is anchored
    and only the remaining work moves to the data date.

    Updates without progress are not a neutral simplification, which is what
    the first draft of this file assumed. An update whose data date advanced a
    fortnight with nothing recorded as done reports a fortnight of slip, and it
    is *right* to: the work did not happen and now starts later. Modelling
    progress is what separates a duration change from time simply passing.
    """
    tasks = []
    for i, d in enumerate(durations):
        if i < complete:
            started, finished = (actuals or {}).get(i, PLANNED[i])
            tasks.append(
                Task(
                    f"A{i}",
                    f"Activity {i}",
                    d,
                    "5D",
                    actual_start=started,
                    actual_finish=finished,
                    remaining_days=0,
                )
            )
        else:
            tasks.append(Task(f"A{i}", f"Activity {i}", d, "5D"))
    links = [Link(f"A{i}", f"A{i + 1}", RelationType.FS, 0) for i in range(len(durations) - 1)]
    outcome = schedule_network(tasks, links, {"5D": five_day}, data_date=day)
    return Update(data_date=day, outcome=outcome, tasks=tasks, links=links, **kw)


# -- the invariant ---------------------------------------------------------


def test_the_windows_sum_to_the_whole(five_day) -> None:  # type: ignore[no-untyped-def]
    """Three updates, two windows, and the two have to account for the total.

    A1 grows 5 to 8 days in the first window; A2 grows 5 to 9 in the second.
    Whatever the finish moves across the series, no window may absorb or invent
    any part of it.
    """
    updates = [
        update(JUN1, [5, 5, 5], five_day),
        update(date(2026, 6, 8), [5, 8, 5], five_day, complete=1),
        update(date(2026, 6, 22), [5, 8, 9], five_day, complete=2, actuals={1: A1_ACTUAL}),
    ]
    analysis = analyse(updates)

    assert len(analysis.windows) == 2
    assert analysis.windows_sum
    assert sum(w.slip_days for w in analysis.windows) == analysis.total_slip_days
    assert analysis.windows[0].slip_days > 0
    assert analysis.windows[1].slip_days > 0


def test_the_windows_sum_under_randomly_perturbed_series(five_day) -> None:  # type: ignore[no-untyped-def]
    """The property, over series nobody chose.

    Durations grow and shrink at random across six updates, so windows recover
    time as well as lose it -- which is where an analysis that quietly drops
    negatives stops adding up.
    """
    rng = random.Random(20260814)
    for _ in range(40):
        durations = [rng.randint(1, 10) for _ in range(4)]
        updates = []
        day = JUN1
        for _ in range(6):
            updates.append(update(day, list(durations), five_day))
            day += timedelta(days=14)
            index = rng.randrange(len(durations))
            durations[index] = max(1, durations[index] + rng.randint(-3, 5))

        analysis = analyse(updates)
        assert analysis.windows_sum, (
            f"windows totalled {sum(w.slip_days for w in analysis.windows)} against an "
            f"overall {analysis.total_slip_days}"
        )
        assert analysis.issues.entries == [] or not analysis.issues.has("WINDOWS.DO_NOT_SUM")


def test_a_window_that_recovered_time_is_reported_as_a_negative(five_day) -> None:  # type: ignore[no-untyped-def]
    """Acceleration is a fact. Counting only the slips overstates the claim.

    A1 was carrying ten days at the June baseline, putting the finish at Fri 26
    June. By the 8th, A0 is done and A1 has been re-planned to four days, which
    pulls the finish back to Thu 18 June -- eight calendar days recovered.
    """
    updates = [
        update(JUN1, [5, 10, 5], five_day),
        update(date(2026, 6, 8), [5, 4, 5], five_day, complete=1),
    ]
    analysis = analyse(updates)

    assert analysis.first_finish == date(2026, 6, 26)
    assert analysis.last_finish == date(2026, 6, 18)
    assert analysis.windows[0].slip_days == -8
    assert analysis.total_slip_days == -8
    assert analysis.windows_sum
    assert analysis.worst is None, "nothing slipped, so there is no worst window"


# -- what it refuses -------------------------------------------------------


def test_a_single_update_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(WindowsError, match="at least two updates"):
        analyse([update(JUN1, [5], five_day)])


def test_two_updates_on_the_same_data_date_are_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    """There is no window between them to measure."""
    with pytest.raises(WindowsError, match="share the data date"):
        analyse([update(JUN1, [5], five_day), update(JUN1, [7], five_day)])


def test_updates_out_of_order_are_refused_rather_than_sorted(five_day) -> None:  # type: ignore[no-untyped-def]
    """Sorting would move a delay into a window it did not happen in.

    Silently repairing the input is the failure mode that matters here: the
    analysis still produces a number, the number is wrong, and nothing in the
    output says which window the delay was moved out of.
    """
    with pytest.raises(WindowsError, match="run backwards"):
        analyse(
            [
                update(date(2026, 7, 1), [5], five_day),
                update(JUN1, [7], five_day),
            ]
        )


# -- what it reports -------------------------------------------------------


def test_each_window_is_measured_against_its_predecessor_not_the_baseline(five_day) -> None:  # type: ignore[no-untyped-def]
    """The chain telescopes only if every window starts where the last ended."""
    updates = [
        update(JUN1, [5, 5, 5], five_day),
        update(date(2026, 6, 8), [5, 8, 5], five_day, complete=1),
        update(date(2026, 6, 22), [5, 8, 9], five_day, complete=2, actuals={1: A1_ACTUAL}),
    ]
    analysis = analyse(updates)

    first, second = analysis.windows
    assert first.closing_finish == second.opening_finish
    assert first.opening_finish == analysis.first_finish
    assert second.closing_finish == analysis.last_finish


def test_the_worst_window_is_named_and_ties_break_on_the_earlier_one(five_day) -> None:  # type: ignore[no-untyped-def]
    """Two windows lose the same five days; the earlier one is where to look.

    It is the one whose cause was still available to be managed.

    Mon 8 June: A0 is done as planned and A1 has grown 5 to 8 days, moving the
    finish from Fri 19 to Wed 24 June. Mon 22 June: A1 has finished on the 17th
    as that update predicted, and A2 has grown 5 to 6 days -- Wed 24 to Mon 29.
    Five calendar days each time.
    """
    updates = [
        update(JUN1, [5, 5, 5], five_day),
        update(date(2026, 6, 8), [5, 8, 5], five_day, complete=1),
        update(date(2026, 6, 22), [5, 8, 6], five_day, complete=2, actuals={1: A1_ACTUAL}),
    ]
    analysis = analyse(updates)

    assert analysis.first_finish == date(2026, 6, 19)
    assert analysis.last_finish == date(2026, 6, 29)
    assert analysis.total_slip_days == 10
    assert analysis.windows[0].slip_days == analysis.windows[1].slip_days == 5
    assert analysis.windows_sum
    assert analysis.worst is not None
    assert analysis.worst.index == 0


def test_a_series_that_never_slipped_has_no_worst_window(five_day) -> None:  # type: ignore[no-untyped-def]
    """`None`, not window zero. A worst window that did not lose time reads as
    a finding and is an artefact of taking a maximum over an empty set."""
    updates = [
        update(JUN1, [5, 5, 5], five_day),
        update(date(2026, 6, 8), [5, 5, 5], five_day, complete=1),
    ]
    analysis = analyse(updates)

    assert analysis.first_finish == analysis.last_finish == date(2026, 6, 19)
    assert analysis.total_slip_days == 0
    assert analysis.worst is None


def test_a_driving_path_change_is_surfaced(five_day) -> None:  # type: ignore[no-untyped-def]
    """The fact a claim usually turns on, so it is a field rather than a search.

    Two parallel chains: B drives at first, then A grows past it and takes over.
    """

    def two_chains(day: date, a: int, b: int) -> Update:
        tasks = [
            Task("A0", "A first", a, "5D"),
            Task("A1", "A second", 1, "5D"),
            Task("B0", "B first", b, "5D"),
            Task("B1", "B second", 1, "5D"),
        ]
        links = [
            Link("A0", "A1", RelationType.FS, 0),
            Link("B0", "B1", RelationType.FS, 0),
        ]
        return Update(
            data_date=day,
            outcome=schedule_network(tasks, links, {"5D": five_day}, data_date=day),
            tasks=tasks,
            links=links,
        )

    analysis = analyse([two_chains(JUN1, 3, 10), two_chains(date(2026, 6, 15), 20, 10)])
    assert analysis.windows[0].driving_path_changed
    assert analysis.summary()["path_changes"] == 1


def test_the_causes_sum_to_the_total_slip(five_day) -> None:  # type: ignore[no-untyped-def]
    """`by_cause` includes the residual causes, so it adds up like the windows.

    Reporting only the causes that flatter the analysis is exactly what the
    sum exists to prevent.
    """
    updates = [
        update(JUN1, [5, 5, 5], five_day),
        update(date(2026, 6, 8), [5, 9, 5], five_day, complete=1),
        update(date(2026, 6, 22), [5, 9, 12], five_day, complete=2, actuals={1: A1_ACTUAL}),
    ]
    analysis = analyse(updates)

    assert sum(analysis.by_cause().values()) == analysis.total_slip_days


def test_the_analysis_is_json_safe_and_names_its_method(five_day) -> None:  # type: ignore[no-untyped-def]
    import json

    updates = [
        update(JUN1, [5, 5, 5], five_day),
        update(date(2026, 6, 8), [5, 8, 5], five_day, complete=1),
    ]
    analysis = analyse(updates, match=MatchKey.ID)
    payload = json.loads(json.dumps(analysis.to_dict()))

    assert "MIP 3.3" in payload["method"]
    assert payload["windows_sum"] is True
    assert len(payload["windows"]) == 1


# -- reachable through the API ---------------------------------------------


def test_the_api_endpoint_runs_a_series_and_refuses_a_bad_one() -> None:
    """The engine is only useful if it is reachable, and only safe if the
    refusals survive the trip out.

    A `WindowsError` that reached the caller as a 500 would read as a bug in
    the tool rather than a fixable problem with their updates.
    """
    from massingplan.api.errors import ValidationFailed
    from massingplan.api.schedules import analyse_windows

    def payload(day: str, first: int) -> dict:
        return {
            "data_date": day,
            "activities": [
                {"id": "A0", "duration_days": first},
                {"id": "A1", "duration_days": 5, "predecessors": ["A0"]},
            ],
        }

    result = analyse_windows({"updates": [payload("2026-06-01", 5), payload("2026-06-15", 9)]})
    assert result["window_count"] == 1
    assert result["windows_sum"] is True
    assert "MIP 3.3" in result["method"]

    with pytest.raises(ValidationFailed, match="at least two"):
        analyse_windows({"updates": [payload("2026-06-01", 5)]})

    with pytest.raises(ValidationFailed, match="run backwards"):
        analyse_windows({"updates": [payload("2026-07-01", 5), payload("2026-06-01", 9)]})

    with pytest.raises(ValidationFailed, match="data_date"):
        analyse_windows({"updates": [{"activities": []}, payload("2026-06-15", 9)]})
