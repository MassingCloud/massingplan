"""The calendar adjoint invariant, swept exhaustively.

This is the kernel test. It has its own CI job and a 100% branch coverage floor
on ``core/timeaxis.py``, because every other module in the engine assumes this
invariant without checking it, and a violation is invisible: the dates stay
plausible and nothing raises.

The sweep is nested loops rather than a property-testing DSL on purpose. A
failure has to be readable by somebody who has never seen this file -- "mask
{0,1,2,3,4}, holidays SHUTDOWN_14, i = 2026-12-18, n = 7" is a bug report; a
minimised Hypothesis counterexample plus a shrinking log is a research project.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from massingplan.core.timeaxis import (
    SEVEN_DAY,
    SIX_DAY,
    STANDARD_5_DAY,
    EmptyCalendarError,
    TimeAxisError,
    TimeAxisWindowError,
    WorkCalendar,
    WorkPattern,
    bind_window,
    day_of,
    instant_of,
    standard_calendar,
)

pytestmark = pytest.mark.kernel

WINDOW_FIRST = date(2026, 1, 1)
WINDOW_LAST = date(2027, 12, 31)


def _holiday_shapes() -> dict[str, frozenset[date]]:
    """Four holiday patterns, each chosen to break a different naive implementation."""
    shutdown = frozenset(date(2026, 12, 21) + timedelta(days=k) for k in range(14))
    every_monday = frozenset(
        d for d in (date(2026, 1, 1) + timedelta(days=k) for k in range(730)) if d.weekday() == 0
    )
    return {
        "none": frozenset(),
        "single": frozenset({date(2026, 7, 3)}),
        "shutdown_14": shutdown,
        "every_monday": every_monday,
    }


def _weekday_masks() -> dict[str, frozenset[int]]:
    """All seven contiguous working weeks, from Monday-only to the full seven days."""
    return {f"mask_{n}": frozenset(range(n)) for n in range(1, 8)}


def _calendars() -> list[WorkCalendar]:
    cals = []
    for mask_name, mask in _weekday_masks().items():
        for holiday_name, holidays in _holiday_shapes().items():
            cal = WorkCalendar(
                id=f"{mask_name}/{holiday_name}",
                name=f"{mask_name} {holiday_name}",
                pattern=WorkPattern(working_weekdays=mask, holidays=holidays),
            )
            # A Monday-only calendar whose holidays are every Monday has no
            # working days at all. That is a legitimate configuration to reject,
            # and it is tested separately below rather than swept.
            if mask == frozenset({0}) and holiday_name == "every_monday":
                continue
            cal.bind(WINDOW_FIRST, WINDOW_LAST)
            cals.append(cal)
    return cals


CALENDARS = _calendars()


def test_the_sweep_actually_covers_something() -> None:
    """Guard the guard: if the calendar list is empty, every sweep below passes vacuously."""
    assert len(CALENDARS) == 27  # 7 masks x 4 holiday shapes, minus the empty one
    for cal in CALENDARS:
        assert cal.count_working_days(instant_of(WINDOW_FIRST), instant_of(WINDOW_LAST)) > 0


#: The step sizes swept. 44 is the DCMA high-float threshold; 365 and 400 push
#: past a year so a holiday pattern that repeats annually cannot alias.
STEPS = (0, 1, 2, 3, 5, 8, 13, 21, 44, 100, 200, 365, 400)


@pytest.mark.parametrize("cal", CALENDARS, ids=lambda c: c.id)
def test_add_and_sub_are_exact_inverses(cal: WorkCalendar) -> None:
    """sub(add(i, n), n) == i and add(sub(i, n), n) == i, on the lattice.

    Steps that would run off the bound window are skipped rather than asserted
    on -- a Monday-only calendar has about 200 working days in a four-year
    window, so ``n = 400`` is not a bug, it is out of range, and running off the
    end is tested by name elsewhere. ``checked`` guards against the skip logic
    swallowing the whole sweep.
    """
    probes = [instant_of(WINDOW_FIRST + timedelta(days=k)) for k in range(0, 500, 7)]
    checked = 0
    for raw in probes:
        i = cal.snap_start_forward(raw)
        forward_room = cal.working_days_after(i)
        backward_room = cal.working_days_before(i)
        for n in STEPS:
            if n <= forward_room:
                there = cal.add_working_days(i, n)
                assert cal.sub_working_days(there, n) == i, (
                    f"{cal.id}: add then sub by {n} from {day_of(i)} landed on "
                    f"{day_of(cal.sub_working_days(there, n))}"
                )
                checked += 1
            if n <= backward_room:
                back = cal.sub_working_days(i, n)
                assert cal.add_working_days(back, n) == i, (
                    f"{cal.id}: sub then add by {n} from {day_of(i)} landed on "
                    f"{day_of(cal.add_working_days(back, n))}"
                )
                checked += 1
    assert checked > 500, f"{cal.id}: the sweep only made {checked} assertions"


@pytest.mark.parametrize("cal", CALENDARS, ids=lambda c: c.id)
def test_count_inverts_add(cal: WorkCalendar) -> None:
    """count(i, add(i, n)) == n, which is what makes float measurable."""
    probes = [instant_of(WINDOW_FIRST + timedelta(days=k)) for k in range(0, 500, 11)]
    checked = 0
    for raw in probes:
        i = cal.snap_start_forward(raw)
        room = cal.working_days_after(i)
        for n in STEPS:
            if n > room:
                continue
            assert cal.count_working_days(i, cal.add_working_days(i, n)) == n, (
                f"{cal.id}: count from {day_of(i)} over {n} working days disagreed"
            )
            checked += 1
    assert checked > 200, f"{cal.id}: the sweep only made {checked} assertions"


@pytest.mark.parametrize("cal", CALENDARS, ids=lambda c: c.id)
def test_finish_lands_on_a_working_day(cal: WorkCalendar) -> None:
    """The day before a finish boundary is always a day somebody worked."""
    probes = [instant_of(WINDOW_FIRST + timedelta(days=k)) for k in range(0, 400, 13)]
    for raw in probes:
        start = cal.snap_start_forward(raw)
        for d in range(1, 40):
            finish = cal.finish_from_start(start, d)
            assert cal.is_working(finish - 1), (
                f"{cal.id}: {d} days from {day_of(start)} finished at "
                f"{day_of(finish)}, whose previous day is not a working day"
            )
            assert cal.count_working_days(start, finish) == d


@pytest.mark.parametrize("cal", CALENDARS, ids=lambda c: c.id)
def test_zero_duration_is_an_instant_not_a_day(cal: WorkCalendar) -> None:
    """A milestone starts and finishes at the same instant. Clamping it to 1 is the bug."""
    i = cal.snap_start_forward(instant_of(date(2026, 6, 1)))
    assert cal.finish_from_start(i, 0) == i
    assert cal.start_from_finish(i, 0) == i
    assert cal.count_working_days(i, i) == 0


@pytest.mark.parametrize("cal", CALENDARS, ids=lambda c: c.id)
def test_start_from_finish_inverts_finish_from_start(cal: WorkCalendar) -> None:
    probes = [instant_of(WINDOW_FIRST + timedelta(days=k)) for k in range(0, 400, 17)]
    for raw in probes:
        start = cal.snap_start_forward(raw)
        for d in (1, 2, 7, 30):
            finish = cal.finish_from_start(start, d)
            assert cal.start_from_finish(finish, d) == start


@pytest.mark.parametrize("cal", CALENDARS, ids=lambda c: c.id)
def test_snapping_is_idempotent(cal: WorkCalendar) -> None:
    probes = [instant_of(WINDOW_FIRST + timedelta(days=k)) for k in range(0, 300, 3)]
    for i in probes:
        f = cal.snap_start_forward(i)
        assert cal.snap_start_forward(f) == f
        b = cal.snap_start_back(i)
        assert cal.snap_start_back(b) == b
        ff = cal.snap_finish_forward(i)
        assert cal.snap_finish_forward(ff) == ff
        fb = cal.snap_finish_back(i)
        assert cal.snap_finish_back(fb) == fb


@pytest.mark.parametrize("cal", CALENDARS, ids=lambda c: c.id)
def test_count_is_signed_and_antisymmetric(cal: WorkCalendar) -> None:
    """Negative float depends on this: count(b, a) == -count(a, b), never clamped."""
    a = cal.snap_start_forward(instant_of(date(2026, 3, 2)))
    b = cal.add_working_days(a, 12)
    assert cal.count_working_days(a, b) == 12
    assert cal.count_working_days(b, a) == -12


# -- specific hand-checked cases ------------------------------------------


def test_a_five_day_task_starting_monday_finishes_friday() -> None:
    cal = standard_calendar()
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    monday = instant_of(date(2026, 6, 1))
    assert date(2026, 6, 1).weekday() == 0
    finish = cal.finish_from_start(monday, 5)
    assert day_of(finish - 1) == date(2026, 6, 5)  # the Friday, last day worked
    assert day_of(finish) == date(2026, 6, 6)  # the half-open boundary


def test_a_one_day_task_starting_monday_finishes_monday() -> None:
    cal = standard_calendar()
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    monday = instant_of(date(2026, 6, 1))
    assert day_of(cal.finish_from_start(monday, 1) - 1) == date(2026, 6, 1)


def test_five_working_days_from_friday_skips_the_weekend() -> None:
    cal = standard_calendar()
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    friday = instant_of(date(2026, 6, 5))
    assert date(2026, 6, 5).weekday() == 4
    assert day_of(cal.add_working_days(friday, 1)) == date(2026, 6, 8)
    assert day_of(cal.add_working_days(friday, 5)) == date(2026, 6, 12)


def test_the_cross_calendar_divergence_that_working_day_offsets_hide() -> None:
    """The concrete failure that made the ordinal axis non-negotiable.

    1 June 2026 is a Monday. Five working days on from it:

    * Mon-Fri with a holiday on Mon 8 June -- 1, 2, 3, 4, 5, then 8 is out, so
      index 5 is **Tue 9 June**.
    * Mon-Sat, no holidays -- 1, 2, 3, 4, 5, so index 5 is **Sat 6 June**.

    An engine working in offsets computes ``max(5, 5) = 5`` and has just
    declared those two dates equal. Three days here, and it widens with every
    additional holiday: the divergence is unbounded and silent.
    """
    five_day = WorkCalendar(
        id="A",
        name="Mon-Fri with a holiday",
        pattern=WorkPattern(STANDARD_5_DAY.working_weekdays, frozenset({date(2026, 6, 8)})),
    )
    six_day = WorkCalendar(id="B", name="Mon-Sat", pattern=SIX_DAY)
    bind_window([five_day, six_day], WINDOW_FIRST, WINDOW_LAST)

    start = instant_of(date(2026, 6, 1))
    a_at_5 = five_day.add_working_days(five_day.snap_start_forward(start), 5)
    b_at_5 = six_day.add_working_days(six_day.snap_start_forward(start), 5)

    assert day_of(a_at_5) == date(2026, 6, 9)
    assert day_of(b_at_5) == date(2026, 6, 6)
    # Three calendar days apart -- and identical as offsets.
    assert a_at_5 - b_at_5 == 3


def test_a_two_week_shutdown_pushes_the_finish_by_ten_working_days() -> None:
    """The specific error a missing calendar-exception parser produces."""
    plain = standard_calendar("PLAIN")
    shutdown = WorkCalendar(
        id="XMAS",
        name="Mon-Fri with a Christmas shutdown",
        pattern=WorkPattern(
            STANDARD_5_DAY.working_weekdays,
            frozenset(date(2026, 12, 21) + timedelta(days=k) for k in range(14)),
        ),
    )
    bind_window([plain, shutdown], WINDOW_FIRST, WINDOW_LAST)

    start = instant_of(date(2026, 12, 14))
    plain_finish = plain.finish_from_start(start, 20)
    shutdown_finish = shutdown.finish_from_start(start, 20)
    # Ten working days of shutdown = fourteen calendar days.
    assert shutdown_finish - plain_finish == 14


def test_an_extra_work_day_beats_a_holiday_on_the_same_date() -> None:
    """A planner saying "we are working that Saturday" is the more specific statement."""
    saturday = date(2026, 6, 6)
    cal = WorkCalendar(
        id="MAKEUP",
        name="Mon-Fri plus a make-up Saturday",
        pattern=WorkPattern(
            STANDARD_5_DAY.working_weekdays,
            holidays=frozenset({saturday}),
            extra_work_days=frozenset({saturday}),
        ),
    )
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    assert cal.is_working(instant_of(saturday))


def test_seven_day_calendar_never_skips() -> None:
    cal = WorkCalendar(id="7D", name="Seven day", pattern=SEVEN_DAY)
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    i = instant_of(date(2026, 6, 1))
    for n in range(0, 60):
        assert cal.add_working_days(i, n) == i + n


# -- failure modes ---------------------------------------------------------


def test_a_calendar_with_no_working_days_refuses_to_bind() -> None:
    cal = WorkCalendar(id="NEVER", name="No working days", pattern=WorkPattern(frozenset()))
    with pytest.raises(EmptyCalendarError, match="NEVER"):
        cal.bind(WINDOW_FIRST, WINDOW_LAST)


def test_adding_from_a_non_working_day_raises_rather_than_guessing() -> None:
    """Snapping is the caller's decision. Guessing which direction is a silent default."""
    cal = standard_calendar()
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    saturday = instant_of(date(2026, 6, 6))
    with pytest.raises(TimeAxisError, match="not a working day"):
        cal.add_working_days(saturday, 1)
    with pytest.raises(TimeAxisError, match="not a working day"):
        cal.sub_working_days(saturday, 1)


def test_running_off_the_window_raises_rather_than_returning_a_wrong_answer() -> None:
    cal = standard_calendar()
    cal.bind(date(2026, 1, 1), date(2026, 1, 31))
    i = cal.snap_start_forward(instant_of(date(2026, 1, 5)))
    with pytest.raises(TimeAxisWindowError, match="past the window"):
        cal.add_working_days(i, 100_000)
    with pytest.raises(TimeAxisWindowError, match="before the window"):
        cal.sub_working_days(i, 100_000)
    with pytest.raises(TimeAxisWindowError, match="outside the bound window"):
        cal.snap_start_forward(instant_of(date(2050, 1, 1)))


def test_negative_duration_is_rejected() -> None:
    cal = standard_calendar()
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    i = instant_of(date(2026, 6, 1))
    with pytest.raises(TimeAxisError, match="negative duration"):
        cal.finish_from_start(i, -1)
    with pytest.raises(TimeAxisError, match="negative duration"):
        cal.start_from_finish(i, -1)


def test_a_negative_n_delegates_to_the_other_direction() -> None:
    cal = standard_calendar()
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    i = cal.snap_start_forward(instant_of(date(2026, 6, 1)))
    assert cal.add_working_days(i, -3) == cal.sub_working_days(i, 3)
    assert cal.sub_working_days(i, -3) == cal.add_working_days(i, 3)


def test_an_absurd_window_is_refused_by_name() -> None:
    cal = standard_calendar()
    with pytest.raises(TimeAxisError, match="refusing to build"):
        cal.bind(date(1000, 1, 1), date(2500, 1, 1))


def test_binding_twice_widens_rather_than_replaces() -> None:
    """A second bind must not shrink the window a previous caller relies on."""
    cal = standard_calendar()
    cal.bind(date(2026, 6, 1), date(2026, 6, 30))
    cal.bind(date(2026, 6, 10), date(2026, 6, 20))  # narrower
    # The original window is still queryable.
    assert cal.is_working(instant_of(date(2026, 6, 1)))
    assert cal.snap_start_forward(instant_of(date(2026, 6, 1))) == instant_of(date(2026, 6, 1))
    cal.bind(date(2026, 1, 1), date(2027, 12, 31))  # wider
    assert cal.snap_start_forward(instant_of(date(2027, 6, 1))) == instant_of(date(2027, 6, 1))


def test_an_unbound_calendar_binds_itself_around_today() -> None:
    """Poking a single date should not require knowing about windows."""
    cal = standard_calendar("LAZY")
    today = date.today()
    assert cal.is_working(instant_of(today)) in (True, False)
    assert cal.snap_start_forward(instant_of(today)) >= instant_of(today)


def test_bind_window_accepts_a_mapping_or_an_iterable() -> None:
    a = standard_calendar("A")
    b = standard_calendar("B")
    bind_window({"a": a}, WINDOW_FIRST, WINDOW_LAST)
    bind_window([b], WINDOW_FIRST, WINDOW_LAST)
    for cal in (a, b):
        assert cal.snap_start_forward(instant_of(date(2027, 6, 1)))


def test_instant_and_day_round_trip() -> None:
    for k in range(0, 800, 7):
        d = WINDOW_FIRST + timedelta(days=k)
        assert day_of(instant_of(d)) == d


def test_snap_finish_uses_the_previous_day_not_the_boundary_day() -> None:
    """A finish boundary on Saturday belongs to Friday's work, not Monday's."""
    cal = standard_calendar()
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    saturday_boundary = instant_of(date(2026, 6, 6))
    # Friday 5 June was worked, so the Saturday boundary is already valid.
    assert cal.snap_finish_back(saturday_boundary) == saturday_boundary
    assert cal.snap_finish_forward(saturday_boundary) == saturday_boundary
    # Sunday 7 June: the last worked day before it is still Friday.
    sunday_boundary = instant_of(date(2026, 6, 7))
    assert cal.snap_finish_back(sunday_boundary) == saturday_boundary


def test_snapping_past_the_last_working_day_in_the_window_raises() -> None:
    """The window can end on a non-working day; snapping forward then has nowhere to go.

    Reachable in practice on a sparse calendar -- a Monday-only shift pattern
    whose padded window happens to close mid-week. Returning the *previous*
    Monday here would be a wrong answer dressed as a snap.
    """
    cal = WorkCalendar(id="MON", name="Mondays only", pattern=WorkPattern(frozenset({0})))
    cal.bind(date(2026, 6, 1), date(2026, 6, 3))
    _first_working, last_working = cal.window_bounds()
    assert day_of(last_working).weekday() == 0
    # Any day after the final Monday but still inside the padded window.
    beyond = last_working + 1
    with pytest.raises(TimeAxisWindowError, match="no working day at or after"):
        cal.snap_start_forward(beyond)


def test_snapping_before_the_first_working_day_in_the_window_raises() -> None:
    cal = WorkCalendar(id="MON2", name="Mondays only", pattern=WorkPattern(frozenset({0})))
    cal.bind(date(2026, 6, 1), date(2026, 6, 3))
    first_working, _last = cal.window_bounds()
    before = first_working - 1
    with pytest.raises(TimeAxisWindowError, match="no working day at or before"):
        cal.snap_start_back(before)


def test_room_queries_report_zero_outside_the_lattice_rather_than_negative() -> None:
    """``working_days_after`` past the end is 0 available steps, not a negative count."""
    cal = standard_calendar("ROOM")
    cal.bind(date(2026, 6, 1), date(2026, 6, 30))
    first_working, last_working = cal.window_bounds()
    assert cal.working_days_after(last_working + 5) == 0
    assert cal.working_days_before(first_working - 5) == 0
    # And inside the lattice they are the real counts, so the guard above is not
    # quietly turning every query into 0.
    assert cal.working_days_after(first_working) > 300
    assert cal.working_days_before(last_working) > 300


def test_snapping_at_the_very_edge_of_the_window_raises_not_wraps() -> None:
    cal = WorkCalendar(
        id="EDGE",
        name="Only one working day in the window",
        pattern=WorkPattern(frozenset({0})),
    )
    cal.bind(date(2026, 6, 1), date(2026, 6, 2))
    lattice_first = cal.snap_start_forward(instant_of(date(2025, 6, 2)))
    assert cal.is_working(lattice_first)
    with pytest.raises(TimeAxisWindowError):
        cal.snap_start_back(instant_of(date(2023, 1, 1)))
