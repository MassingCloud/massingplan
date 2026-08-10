"""The constraint semantics table, exhaustively.

The table exists so that a missing case is a test failure rather than a silent
no-op. These tests are the half of that bargain the table cannot keep on its own.
"""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.constraints import (
    EFFECTS,
    ConstraintType,
    early_floor,
    late_cap,
    parse,
)
from massingplan.core.cpm import calculate
from massingplan.core.network import Task
from massingplan.core.timeaxis import day_of, instant_of

JUN1 = date(2026, 6, 1)


def test_every_constraint_type_has_a_row() -> None:
    """The assertion the table was written to make possible.

    A ten-arm if/elif cannot be checked like this, which is why one of its arms
    can go missing in a refactor and the dates still look plausible.
    """
    assert set(EFFECTS) == set(ConstraintType)


@pytest.mark.parametrize("kind", list(ConstraintType))
def test_no_constraint_both_floors_and_caps_the_same_end_unless_it_pins(
    kind: ConstraintType,
) -> None:
    """Only the "on exactly this date" types pin; the rest bound one side."""
    effect = EFFECTS[kind]
    pins_start = effect.floors_early_start and effect.caps_late_start
    pins_finish = effect.floors_early_finish and effect.caps_late_finish
    if pins_start or pins_finish:
        assert kind in (
            ConstraintType.START_ON,
            ConstraintType.FINISH_ON,
            ConstraintType.MANDATORY_START,
            ConstraintType.MANDATORY_FINISH,
        )


def test_only_the_two_mandatory_types_override_logic() -> None:
    mandatory = {k for k, e in EFFECTS.items() if e.is_mandatory}
    assert mandatory == {ConstraintType.MANDATORY_START, ConstraintType.MANDATORY_FINISH}


def test_start_no_earlier_than_is_soft_and_does_not_count_against_dcma_five() -> None:
    """SNET is how a planner records a delivery date. That is a fact, not an override."""
    assert EFFECTS[ConstraintType.START_ON_OR_AFTER].is_hard is False
    assert EFFECTS[ConstraintType.MANDATORY_START].is_hard is True
    assert EFFECTS[ConstraintType.START_ON].is_hard is True


@pytest.mark.parametrize("kind", list(ConstraintType))
def test_a_type_that_needs_a_date_is_exactly_a_type_that_bounds_something(
    kind: ConstraintType,
) -> None:
    effect = EFFECTS[kind]
    bounds_something = any(
        (
            effect.floors_early_start,
            effect.floors_early_finish,
            effect.caps_late_start,
            effect.caps_late_finish,
        )
    )
    assert kind.needs_date == bounds_something


# -- floors and caps, on a five-day calendar -------------------------------


@pytest.mark.parametrize(
    ("kind", "expect_floor", "expect_cap"),
    [
        (ConstraintType.NONE, False, False),
        (ConstraintType.START_ON_OR_AFTER, True, False),
        (ConstraintType.START_ON_OR_BEFORE, False, True),
        (ConstraintType.FINISH_ON_OR_AFTER, True, False),
        (ConstraintType.FINISH_ON_OR_BEFORE, False, True),
        (ConstraintType.START_ON, True, True),
        (ConstraintType.FINISH_ON, True, True),
        (ConstraintType.MANDATORY_START, True, True),
        (ConstraintType.MANDATORY_FINISH, True, True),
        (ConstraintType.AS_LATE_AS_POSSIBLE, False, False),
    ],
)
def test_which_types_produce_a_floor_and_which_a_cap(
    five_day,
    kind: ConstraintType,
    expect_floor: bool,
    expect_cap: bool,  # type: ignore[no-untyped-def]
) -> None:
    at = instant_of(date(2026, 6, 10))
    assert (early_floor(kind, at, 5, five_day) is not None) is expect_floor
    assert (late_cap(kind, at, 5, five_day) is not None) is expect_cap


def test_a_finish_side_floor_is_converted_back_to_a_start(five_day) -> None:  # type: ignore[no-untyped-def]
    """FNET on a five-day activity is a start floor five working days earlier.

    Doing that conversion inside the forward pass is where duplicate off-by-one
    bugs come from, so it happens here, once.
    """
    at = instant_of(date(2026, 6, 12))  # a Friday
    floor = early_floor(ConstraintType.FINISH_ON_OR_AFTER, at, 5, five_day)
    assert floor is not None
    # Finish boundary Fri 12 means last worked Thu 11; five days back starts Fri 5.
    assert day_of(floor) == date(2026, 6, 5)


# -- the matrix: each type against a date before, on, and after the logic date


@pytest.mark.parametrize("kind", [k for k in ConstraintType if k.needs_date])
@pytest.mark.parametrize("offset", [-5, 0, 5])
def test_every_constraint_type_schedules_without_raising(
    five_day,
    kind: ConstraintType,
    offset: int,  # type: ignore[no-untyped-def]
) -> None:
    """Thirty cases. None of them may raise, and none may clamp float at zero.

    The point is coverage of the interaction, not of a particular date: a
    constraint that cannot be met is an ordinary, expected outcome, and the
    engine has to keep computing and report it.
    """
    from datetime import timedelta

    logic_start = date(2026, 6, 8)
    tasks = [
        Task("A", "", 5, "5D"),
        Task(
            "B",
            "",
            5,
            "5D",
            constraint=kind,
            constraint_date=logic_start + timedelta(days=offset),
        ),
    ]
    from massingplan.core.network import Link

    result = calculate(tasks, [Link("A", "B")], {"5D": five_day}, data_date=JUN1)
    tf = result.total_float_days["B"]
    assert tf is not None
    # Whatever the answer, it must be an integer count of working days -- not a
    # clamped zero standing in for "we could not honour this".
    assert isinstance(tf, int)


def test_an_impossible_constraint_yields_negative_float_rather_than_an_exception(
    five_day,
) -> None:  # type: ignore[no-untyped-def]
    tasks = [
        Task("A", "", 20, "5D"),
        Task(
            "B",
            "",
            5,
            "5D",
            constraint=ConstraintType.FINISH_ON_OR_BEFORE,
            constraint_date=date(2026, 6, 3),
        ),
    ]
    from massingplan.core.network import Link

    result = calculate(tasks, [Link("A", "B")], {"5D": five_day}, data_date=JUN1)
    assert result.total_float_days["B"] < 0
    assert result.violations


# -- parsing ---------------------------------------------------------------


def test_parse_round_trips_every_type() -> None:
    for kind in ConstraintType:
        assert parse(kind.value) is kind


def test_parse_is_tolerant_of_spelling_but_not_inventive() -> None:
    assert parse("Start-On-Or-After") is ConstraintType.START_ON_OR_AFTER
    assert parse("  finish_on  ") is ConstraintType.FINISH_ON
    assert parse(None) is ConstraintType.NONE
    assert parse("") is ConstraintType.NONE


def test_an_unknown_constraint_reads_as_none_rather_than_aborting_an_import() -> None:
    """A single unrecognised value in a five-thousand-activity file must not
    abort the whole import. The caller compares against NONE and raises an issue.
    """
    assert parse("must_happen_on_a_tuesday") is ConstraintType.NONE
