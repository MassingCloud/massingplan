"""Takt planning, checked by hand.

Every expected value here is written out, not generated. A takt plan is
arithmetic simple enough that a planner can verify it on the back of a drawing,
which is most of the argument for the method -- so the tests verify it the same
way.

The two properties worth stating up front, because they are what separate takt
from the line-of-balance engine next door:

* the duration is `(wagons + zones - 1) * takt`, known before any of the work
  is estimated;
* the crews move so the durations do not, and the slack that creates is
  reported rather than absorbed.
"""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.issues import Severity
from massingplan.core.locations import Location
from massingplan.core.network import RelationType
from massingplan.core.schedule import schedule_network
from massingplan.core.takt import (
    TaktError,
    Wagon,
    crews_for,
    minimum_takt,
    plan,
    to_network,
)
from massingplan.core.timeaxis import standard_calendar

FLOORS = [Location(id=f"L{n}", name=f"Level {n}", sequence=n - 1) for n in range(1, 5)]


def _train() -> list[Wagon]:
    """Four trades, four floors, a five-day takt.

    Work content in crew-days per floor, chosen so each wagon exercises a
    different case:

    * Frame      8.0 -> 2 crews, 8/10  = 80% used
    * MEP       12.0 -> 3 crews, 12/15 = 80% used
    * Drywall    5.0 -> 1 crew,  5/5   = 100%, a full wagon
    * Paint      2.0 -> 1 crew,  2/5   = 40%, the honest waste
    """
    return [
        Wagon(id="Frame", default_work=8.0, max_crews=4),
        Wagon(id="MEP", default_work=12.0, max_crews=4),
        Wagon(id="Drywall", default_work=5.0, max_crews=4),
        Wagon(id="Paint", default_work=2.0, max_crews=4),
    ]


# -- the crew arithmetic ---------------------------------------------------


@pytest.mark.parametrize(
    ("work", "takt", "expected"),
    [
        (5.0, 5, 1),
        (5.1, 5, 2),
        (10.0, 5, 2),
        (12.0, 5, 3),
        (0.0, 5, 0),
        (0.5, 5, 1),
    ],
)
def test_crews_for_is_a_ceiling_on_whole_crews(work: float, takt: int, expected: int) -> None:
    assert crews_for(work, takt) == expected


@pytest.mark.parametrize(
    ("quantity", "rate", "takt", "expected", "is_trap"),
    [
        # 21 units at 0.7 a day is 30.000000000000004 crew-days. Over a 5-day
        # takt that is 6.000000000000001, and a bare `ceil` hires SEVEN crews
        # where six will do -- a sixth of a trade's labour bill, from a number
        # that is 6 in every sense a planner cares about.
        (21, 0.7, 5, 6, True),
        (21, 0.7, 10, 3, True),  # 3.0000000000000004 -> 3, not 4
        (21, 0.35, 5, 12, True),  # 12.000000000000002 -> 12, not 13
        # The control: genuinely over the boundary, and nowhere near it, so the
        # tolerance is not just rounding everything down.
        (22, 0.7, 5, 7, False),
    ],
)
def test_the_epsilon_stops_a_representation_error_hiring_a_crew(
    quantity: float, rate: float, takt: int, expected: int, is_trap: bool
) -> None:
    """Work content is `quantity / rate`, and that division drifts upward.

    **Division rather than summation, and that is not a cosmetic choice.** The
    first version of this test built the work content with `sum([0.2] * 25)`,
    which is `5.000000000000002` on Python 3.11 and exactly `5.0` on 3.12 --
    Python 3.12 made `sum()` use compensated (Neumaier) summation. The test
    passed locally on 3.11 and failed on CI's 3.12 and 3.13, and what caught it
    was the guard assertion below refusing to claim a drift that was not there.

    IEEE-754 division is exactly rounded and identical on every version, so
    these cases are stable. They also match where the number actually comes
    from: `locations.work_content` divides a take-off by a production rate.
    """
    work = quantity / rate
    over = work / takt
    if is_trap:
        # The drift is real and is asserted, not assumed -- an assertion of this
        # shape is what caught the version dependency described above, by
        # refusing to claim a trap that had stopped existing.
        drift = over - round(over)
        assert 0 < drift < 1e-9, (
            f"{work!r} / {takt} is {over!r}, which is not a hair above an "
            "integer -- this case no longer exercises the epsilon"
        )
    else:
        assert abs(over - round(over)) > 1e-6, "the control must not sit near a boundary"
    assert crews_for(work, takt) == expected


def test_no_work_needs_no_crew_rather_than_one() -> None:
    """A wagon with nothing to do in a zone still occupies the slot -- the train
    cannot leave a gap -- but billing a gang for it would be a fiction.
    """
    assert crews_for(0.0, 5) == 0


def test_a_takt_of_zero_days_is_refused_not_divided_by() -> None:
    with pytest.raises(TaktError):
        crews_for(5.0, 0)


# -- the plan --------------------------------------------------------------


def test_the_duration_is_wagons_plus_zones_minus_one_takts() -> None:
    """The formula that makes the method worth using: four wagons through four
    floors on a five-day takt is (4 + 4 - 1) x 5 = 35 working days, and you can
    say so before estimating any of the work.
    """
    result = plan(_train(), FLOORS, takt_days=5)
    assert result.duration_days == 35


def test_crews_are_sized_so_the_durations_do_not_move() -> None:
    result = plan(_train(), FLOORS, takt_days=5)
    assert result.crews == {"Frame": 2, "MEP": 3, "Drywall": 1, "Paint": 1}
    assert all(slot.crews == result.crews[slot.wagon_id] for slot in result.slots)
    # The durations did not move: every slot is one takt, which is the method.
    assert {
        row["duration_days"]
        for row in result.to_rows(start=date(2026, 3, 2), calendar=standard_calendar())
    } == {5}


def test_utilisation_is_the_price_and_is_not_rounded_up() -> None:
    """The number the method has to be justified against.

    Paint at 2 crew-days inside a 5-day takt is 40% used. Reporting that as
    "1 crew" and moving on is what makes a takt plan look efficient when three
    fifths of a trade's labour is standing about.
    """
    result = plan(_train(), FLOORS, takt_days=5)
    assert result.utilisation["Drywall"] == pytest.approx(1.0)
    assert result.utilisation["Paint"] == pytest.approx(0.4)
    assert result.utilisation["Frame"] == pytest.approx(0.8)
    assert result.utilisation["MEP"] == pytest.approx(0.8)


def test_idle_crew_days_totals_the_waste_in_the_unit_it_is_argued_in() -> None:
    """Per floor: Frame 10-8=2, MEP 15-12=3, Drywall 0, Paint 5-2=3. Eight a
    floor, four floors, thirty-two crew-days paid for and not worked.
    """
    result = plan(_train(), FLOORS, takt_days=5)
    assert result.idle_crew_days == pytest.approx(32.0)


def test_every_wagon_visits_every_zone_exactly_once() -> None:
    result = plan(_train(), FLOORS, takt_days=5)
    assert len(result.slots) == 4 * 4
    for wagon in _train():
        visited = [slot.zone_id for slot in result.by_wagon(wagon.id)]
        assert visited == [loc.id for loc in FLOORS]


def test_wagon_n_enters_zone_z_at_takt_n_plus_z() -> None:
    """The whole scheduling calculation: no search, no shifting, no float."""
    result = plan(_train(), FLOORS, takt_days=5)
    grid = {(slot.wagon_id, slot.zone_id): slot.takt_index for slot in result.slots}
    assert grid[("Frame", "L1")] == 0
    assert grid[("MEP", "L1")] == 1
    assert grid[("Frame", "L2")] == 1
    assert grid[("Paint", "L4")] == 6  # wagon 3 + zone 3, the last takt


def test_two_wagons_never_occupy_the_same_zone_at_the_same_takt() -> None:
    """The constraint the rhythm exists to satisfy, asserted directly rather
    than inferred from the dates.
    """
    result = plan(_train(), FLOORS, takt_days=5)
    occupied = [(slot.zone_id, slot.takt_index) for slot in result.slots]
    assert len(occupied) == len(set(occupied))


# -- what it refuses to do -------------------------------------------------


def test_a_wagon_that_cannot_fit_is_refused_not_squeezed() -> None:
    """Capping the crew count silently produces a wagon that cannot finish
    inside its takt -- which breaks the rhythm everywhere downstream while the
    plan still looks like a takt plan. That is the dangerous shape.
    """
    wagons = [
        Wagon(id="Frame", default_work=8.0, max_crews=4),
        Wagon(id="MEP", default_work=30.0, max_crews=2),
    ]
    result = plan(wagons, FLOORS, takt_days=5)

    assert result.overloaded == ("MEP",)
    assert result.issues.has("TAKT_OVERLOADED")
    assert result.issues.count(Severity.ERROR) == 1
    offender = result.issues.by_severity(Severity.ERROR)[0]
    assert "MEP" in offender.message
    assert "6 crews" in offender.message  # 30 / 5, against a ceiling of 2


def test_a_low_wagon_is_reported_rather_than_left_to_be_noticed() -> None:
    result = plan(_train(), FLOORS, takt_days=5)
    low = [e for e in result.issues.entries if e.code == "TAKT_LOW_UTILISATION"]
    assert [e.row_key for e in low] == ["Paint"]
    assert "40%" in low[0].message


def test_an_empty_wagon_says_so_and_still_takes_its_slot() -> None:
    """A trade with no work anywhere still occupies a slot in every zone,
    because removing it would shorten the train and break the rhythm. The cost
    is real and is named.
    """
    wagons = [Wagon(id="Frame", default_work=5.0), Wagon(id="Snagging", default_work=0.0)]
    result = plan(wagons, FLOORS, takt_days=5)
    assert result.issues.has("TAKT_EMPTY_WAGON")
    assert len(result.by_wagon("Snagging")) == len(FLOORS)
    assert result.crews["Snagging"] == 0
    assert result.duration_days == (2 + 4 - 1) * 5


@pytest.mark.parametrize("bad", [0, -1])
def test_a_takt_shorter_than_a_day_is_refused(bad: int) -> None:
    with pytest.raises(TaktError):
        plan(_train(), FLOORS, takt_days=bad)


def test_duplicate_ids_are_refused_rather_than_silently_merged() -> None:
    with pytest.raises(TaktError, match="duplicate zone"):
        plan(_train(), [*FLOORS, Location(id="L1", sequence=9)], takt_days=5)
    with pytest.raises(TaktError, match="duplicate wagon"):
        plan([*_train(), Wagon(id="Frame")], FLOORS, takt_days=5)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_work_content_that_is_not_a_finite_number_is_refused(bad: float) -> None:
    """`ceil(nan)` raises and `ceil(inf)` overflows, both several layers below
    where anybody would look for the cause.
    """
    with pytest.raises(TaktError):
        Wagon(id="Frame", work_content={"L1": bad})


# -- the bottleneck --------------------------------------------------------


def test_minimum_takt_names_the_wagon_that_sets_it() -> None:
    """ "Three days" is not actionable. "Three days, and it is the M&E that says
    so" is: shortening any other trade changes nothing at all.
    """
    days, bottleneck = minimum_takt(_train(), FLOORS)
    assert (days, bottleneck) == (3, "MEP")  # 12 crew-days over 4 crews


def test_the_minimum_takt_is_actually_feasible() -> None:
    """The property, not just the number: at the takt it returns, nothing is
    overloaded. At one day shorter, something is.
    """
    days, _ = minimum_takt(_train(), FLOORS)
    assert plan(_train(), FLOORS, takt_days=days).overloaded == ()
    assert plan(_train(), FLOORS, takt_days=days - 1).overloaded != ()


def test_raising_the_bottlenecks_crew_ceiling_shortens_the_takt() -> None:
    faster = [
        Wagon(id="Frame", default_work=8.0, max_crews=4),
        Wagon(id="MEP", default_work=12.0, max_crews=6),
        Wagon(id="Drywall", default_work=5.0, max_crews=4),
        Wagon(id="Paint", default_work=2.0, max_crews=4),
    ]
    assert minimum_takt(faster, FLOORS) == (2, "Frame")  # 8 over 4; MEP now fits in 2


# -- the emitted network ---------------------------------------------------


def test_the_plan_schedules_to_the_takt_it_was_given() -> None:
    """The rhythm has to survive the CPM engine, not just this module.

    Without the start constraints, the forward pass pulls the light wagons
    earlier and quietly dissolves the takt -- it would hold here and not in the
    schedule anybody actually reads.
    """
    result = plan(_train(), FLOORS, takt_days=5)
    calendar = standard_calendar()
    tasks, links, calendars = to_network(
        result, _train(), FLOORS, start=date(2026, 3, 2), calendar=calendar
    )
    outcome = schedule_network(tasks, links, calendars)

    rows = {row["activity_id"]: row for row in outcome.to_rows()}
    assert rows["Frame@L1"]["start"] == "2026-03-02"
    # Wagon 0, zone 1 -> takt 1 -> five working days later, Monday to Monday.
    assert rows["Frame@L2"]["start"] == "2026-03-09"
    assert rows["MEP@L1"]["start"] == "2026-03-09"
    # The last slot: takt 6, thirty working days in.
    assert rows["Paint@L4"]["start"] == "2026-04-13"


def test_the_two_dependencies_are_stated_not_implied_by_the_dates() -> None:
    """A network whose logic is carried only by constraint dates reports every
    activity as critical with no float, which is the exact defect the P6
    importer was fixed for.
    """
    result = plan(_train(), FLOORS, takt_days=5)
    _tasks, links, _cals = to_network(result, _train(), FLOORS, start=date(2026, 3, 2))
    pairs = {(link.predecessor, link.successor) for link in links}

    assert ("Frame@L1", "Frame@L2") in pairs, "the crew's own chain is missing"
    assert ("Frame@L1", "MEP@L1") in pairs, "the handover is missing"
    assert all(link.type is RelationType.FS for link in links)
    assert all(link.lag_days == 0 for link in links)


def test_every_slot_has_a_task_and_every_task_a_slot() -> None:
    result = plan(_train(), FLOORS, takt_days=5)
    tasks, _links, _cals = to_network(result, _train(), FLOORS, start=date(2026, 3, 2))
    assert {t.id for t in tasks} == {slot.activity_id for slot in result.slots}


def test_to_rows_reports_the_last_day_worked_not_the_boundary() -> None:
    """The half-open convention, converted at exactly one site. A slot that
    finishes on a Friday has its boundary on the Monday, and a bare `- 1`
    prints Sunday.
    """
    result = plan(_train(), FLOORS, takt_days=5)
    rows = {
        r["activity_id"]: r
        for r in result.to_rows(start=date(2026, 3, 2), calendar=standard_calendar())
    }
    first = rows["Frame@L1"]
    assert first["start"] == "2026-03-02"  # Monday
    assert first["finish"] == "2026-03-06"  # Friday, not the Saturday boundary
    assert date.fromisoformat(str(first["finish"])).weekday() == 4
