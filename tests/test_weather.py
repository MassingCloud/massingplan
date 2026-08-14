"""Weather allowance, worked by hand.

The acceptance criterion from the roadmap is the last test here: a schedule
with the allowance removed finishes earlier by exactly the allowance consumed
on the driving path. That is what separates modelling weather from padding a
duration and hoping.
"""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.model import Calendar, CalendarException
from massingplan.core.network import Link, RelationType, Task
from massingplan.core.schedule import schedule_network
from massingplan.core.weather import (
    WEATHER,
    Allowance,
    WeatherError,
    apply_allowance,
    apply_to_all,
    without_allowance,
)

JUN1 = date(2026, 6, 1)


def mon_fri() -> Calendar:
    return Calendar(id="5D", name="Mon-Fri", working_weekdays={0, 1, 2, 3, 4})


# -- what it adds ----------------------------------------------------------


def test_the_allowance_lands_as_non_working_days_not_longer_durations() -> None:
    """A weather day is a day nobody works, which is what a calendar already
    models. Inside a duration it cannot be argued about or claimed against."""
    calendar, applied = apply_allowance(
        mon_fri(),
        Allowance("5D", {6: 4}),
        start=JUN1,
        finish=date(2026, 6, 30),
    )
    assert applied.total_days == 4
    assert len(calendar.holidays) == 4
    assert all(e.name == WEATHER for e in calendar.exceptions)
    assert applied.by_month() == {"2026-06": 4}


def test_the_days_are_spread_rather_than_clustered() -> None:
    """Where they sit is arbitrary; clustering makes it a lead or a lag.

    June 2026 has 22 working days. Four allowed, spread evenly, land near the
    quarter points -- not all in the first week (which would delay everything
    downstream by four days at once) nor the last (which would delay nothing
    until July).
    """
    _calendar, applied = apply_allowance(
        mon_fri(), Allowance("5D", {6: 4}), start=JUN1, finish=date(2026, 6, 30)
    )
    days = list(applied.days)
    assert len(days) == 4
    assert len(set(days)) == 4, "no day allowed twice"
    spread = (days[-1] - days[0]).days
    assert spread > 14, f"the four days span only {spread} days, which is a cluster"


def test_a_day_already_lost_to_a_shutdown_is_not_lost_twice() -> None:
    """A Christmas holiday cannot also be a weather day.

    Counting it twice inflates the allowance by exactly the length of the
    shutdown, and the schedule still computes.
    """
    calendar = mon_fri()
    for day in range(21, 26):  # Mon 21 to Fri 25 December
        calendar.exceptions.append(
            CalendarException(day=date(2026, 12, day), name="Christmas shutdown")
        )
    # Enough days that the allowance *must* reach into the shutdown week if the
    # guard is not there. A first version asked for three, which spread either
    # side of Christmas and passed whether the guard existed or not -- the
    # sabotage went green and said so.
    updated, applied = apply_allowance(
        calendar, Allowance("5D", {12: 20}), start=date(2026, 12, 1), finish=date(2026, 12, 31)
    )
    shutdown = {date(2026, 12, d) for d in range(21, 26)}
    assert not (set(applied.days) & shutdown), "a shutdown day was allowed as weather too"
    assert applied.total_days == 18, "23 working days in December 2026, less the 5 shut down"
    assert len(updated.holidays) == 5 + 18


def test_a_weekend_is_not_available_to_lose() -> None:
    """Days the calendar does not work cannot be lost to weather."""
    _calendar, applied = apply_allowance(
        mon_fri(), Allowance("5D", {6: 22}), start=JUN1, finish=date(2026, 6, 30)
    )
    assert all(d.weekday() < 5 for d in applied.days)


def test_allowing_more_days_than_the_month_has_takes_the_month() -> None:
    """Not an error -- a month can genuinely be written off -- but it cannot
    take days that are not there."""
    _calendar, applied = apply_allowance(
        mon_fri(), Allowance("5D", {6: 40}), start=JUN1, finish=date(2026, 6, 30)
    )
    assert applied.total_days == 22, "June 2026 has 22 working days"


# -- what it refuses -------------------------------------------------------


def test_a_negative_allowance_is_refused() -> None:
    """Weather does not create working days."""
    with pytest.raises(WeatherError, match="negative allowance"):
        Allowance("5D", {6: -2})


def test_a_month_outside_one_to_twelve_is_refused() -> None:
    with pytest.raises(WeatherError, match="not a month"):
        Allowance("5D", {13: 1})


def test_an_allowance_for_a_calendar_that_is_not_here_is_refused() -> None:
    """Silently allowing nothing is the failure that shows up later as "the
    weather allowance did not seem to do anything"."""
    with pytest.raises(WeatherError, match="not in this schedule"):
        apply_to_all([mon_fri()], [Allowance("6D", {6: 2})], start=JUN1, finish=date(2026, 6, 30))


def test_the_source_calendar_is_never_mutated() -> None:
    """It is shared between the with-allowance and without-allowance runs, and
    editing in place would make the second measure the first."""
    original = mon_fri()
    apply_allowance(original, Allowance("5D", {6: 4}), start=JUN1, finish=date(2026, 6, 30))
    assert original.exceptions == []


# -- removing it again -----------------------------------------------------


def test_removing_the_allowance_keeps_the_shutdown() -> None:
    """Stripping every exception would delete Christmas and report a fortnight
    of it as weather recovered."""
    calendar = mon_fri()
    calendar.exceptions.append(CalendarException(day=date(2026, 12, 25), name="Christmas"))
    with_weather, _ = apply_allowance(
        calendar, Allowance("5D", {12: 2}), start=date(2026, 12, 1), finish=date(2026, 12, 31)
    )
    stripped = without_allowance(with_weather)

    assert date(2026, 12, 25) in stripped.holidays
    assert len(stripped.holidays) == 1
    assert all(e.name != WEATHER for e in stripped.exceptions)


# -- the acceptance criterion ----------------------------------------------


def test_removing_the_allowance_finishes_earlier_by_exactly_what_it_consumed() -> None:
    """The roadmap's acceptance test, and the whole argument for the module.

    A five-activity chain across June and July. With four weather days allowed
    in each month the finish moves out; removing them moves it back by exactly
    the number of allowed days the chain actually crossed -- not by the total
    allowed, because a day allowed after the work has finished costs nothing.

    Padding a duration cannot produce this number at all: once the days are
    inside the estimate there is no version of the schedule without them.
    """
    tasks = [Task(f"A{i}", f"Activity {i}", 8, "5D") for i in range(5)]
    links = [Link(f"A{i}", f"A{i + 1}", RelationType.FS, 0) for i in range(4)]

    plain = mon_fri()
    wet, applied = apply_allowance(
        plain, Allowance("5D", {6: 4, 7: 4}), start=JUN1, finish=date(2026, 8, 31)
    )

    dry_outcome = schedule_network(tasks, links, {"5D": plain.to_work_calendar()}, data_date=JUN1)
    wet_outcome = schedule_network(tasks, links, {"5D": wet.to_work_calendar()}, data_date=JUN1)

    # Only the allowed days the work actually crossed can have cost anything.
    consumed = [
        d for d in applied.days if dry_outcome.project_start <= d <= wet_outcome.project_finish
    ]
    moved = (wet_outcome.project_finish - dry_outcome.project_finish).days

    assert moved > 0, "the allowance has to move the finish or it is not modelled"
    assert wet_outcome.duration_working_days == dry_outcome.duration_working_days, (
        "the work itself did not change; only the days available to do it in"
    )
    # The finish moves by whole calendar days that include the weekends the
    # displaced work now spans, so the working-day count is the exact figure.
    lost_working_days = len(consumed)
    assert lost_working_days == 8
    assert (
        plain.to_work_calendar().count_working_days(
            dry_outcome.project_finish.toordinal(), wet_outcome.project_finish.toordinal()
        )
        == lost_working_days
    )


def test_the_stripped_calendar_reproduces_the_original_schedule_exactly() -> None:
    """`without_allowance` has to be the inverse, or the comparison is between
    two things that differ by more than the weather."""
    tasks = [Task("A", "Walls", 10, "5D"), Task("B", "Roof", 10, "5D")]
    links = [Link("A", "B", RelationType.FS, 0)]

    plain = mon_fri()
    wet, _ = apply_allowance(
        plain, Allowance("5D", {6: 3, 7: 3}), start=JUN1, finish=date(2026, 8, 31)
    )
    restored = without_allowance(wet)

    a = schedule_network(tasks, links, {"5D": plain.to_work_calendar()}, data_date=JUN1)
    b = schedule_network(tasks, links, {"5D": restored.to_work_calendar()}, data_date=JUN1)
    assert a.to_rows() == b.to_rows()


def test_applying_to_all_leaves_an_unallowanced_calendar_alone() -> None:
    six = Calendar(id="6D", name="Mon-Sat", working_weekdays={0, 1, 2, 3, 4, 5})
    calendars, applied = apply_to_all(
        [mon_fri(), six], [Allowance("5D", {6: 2})], start=JUN1, finish=date(2026, 6, 30)
    )
    by_id = {c.id: c for c in calendars}
    assert len(by_id["5D"].holidays) == 2
    assert by_id["6D"].exceptions == []
    assert [a.calendar_id for a in applied] == ["5D"]
