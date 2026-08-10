"""The single rounding site and the date-type guard.

Small module, disproportionate blast radius: every duration in every imported
file passes through ``days_from_hours``, and every date from every database
driver passes through ``as_date``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from massingplan.core.units import (
    HOURS_PER_DAY_DEFAULT,
    ROUNDING_EPSILON,
    UnitError,
    as_date,
    days_from_hours,
    hours_from_days,
    maybe_date,
)


def test_forty_hours_on_an_eight_hour_day_is_five_days_not_six() -> None:
    """The float-precision bug the epsilon exists for.

    ``40 / 8.0`` can land a hair above 5.0 in binary floating point, and a bare
    ``ceil`` then makes a one-week task six days long. Invisible in the output,
    and it reappears every time somebody simplifies the expression.
    """
    assert days_from_hours(40, 8.0) == 5


def test_a_part_day_rounds_up_because_you_cannot_hand_over_half_a_shift() -> None:
    assert days_from_hours(41, 8.0) == 6
    assert days_from_hours(33, 8.0) == 5
    assert days_from_hours(0.5, 8.0) == 1


def test_zero_and_none_are_zero_days_not_one() -> None:
    """A milestone is an instant. Clamping it to a day is one of the four XER traps."""
    assert days_from_hours(0, 8.0) == 0
    assert days_from_hours(None, 8.0) == 0


def test_a_ten_hour_day_calendar_rescales() -> None:
    """The trap: hours per day comes from the file's CALENDAR, never a hardcoded 8."""
    assert days_from_hours(40, 10.0) == 4
    assert days_from_hours(40, 8.0) == 5
    assert days_from_hours(45, 10.0) == 5


def test_the_epsilon_is_small_enough_not_to_swallow_a_real_part_day() -> None:
    """Guard the guard: an epsilon set too large would round 41 hours down to 5."""
    assert ROUNDING_EPSILON < 1e-6
    assert days_from_hours(8.0 + 1e-3, 8.0) == 2


def test_hours_and_days_round_trip_at_the_default() -> None:
    for d in range(0, 40):
        assert days_from_hours(hours_from_days(d, HOURS_PER_DAY_DEFAULT), 8.0) == d


def test_a_nonsensical_calendar_is_rejected_by_name() -> None:
    with pytest.raises(UnitError, match="hours_per_day must be positive"):
        days_from_hours(8, 0)
    with pytest.raises(UnitError, match="hours_per_day must be positive"):
        hours_from_days(1, -8)
    with pytest.raises(UnitError, match="negative duration"):
        days_from_hours(-8, 8.0)


def test_a_midnight_datetime_is_narrowed_because_every_driver_returns_one() -> None:
    assert as_date(datetime(2026, 6, 1, 0, 0), field="start") == date(2026, 6, 1)


def test_a_datetime_with_a_time_of_day_is_rejected_not_truncated() -> None:
    """Truncating silently discards information the caller thought it was passing."""
    with pytest.raises(UnitError, match="carries a time of day"):
        as_date(datetime(2026, 6, 1, 9, 30), field="actual_start")


def test_a_timezone_aware_datetime_is_rejected_at_the_boundary() -> None:
    """Otherwise it raises deep in the backward pass, where the traceback names nothing."""
    with pytest.raises(UnitError, match="timezone-aware"):
        as_date(datetime(2026, 6, 1, tzinfo=timezone.utc), field="data_date")


def test_the_error_names_the_field_it_came_from() -> None:
    with pytest.raises(UnitError, match="constraint_date"):
        as_date("2026-06-01", field="constraint_date")


def test_a_plain_date_passes_through() -> None:
    d = date(2026, 6, 1)
    assert as_date(d, field="x") is d


def test_maybe_date_lets_none_through_for_optional_columns() -> None:
    assert maybe_date(None, field="actual_finish") is None
    assert maybe_date(date(2026, 6, 1), field="actual_finish") == date(2026, 6, 1)
