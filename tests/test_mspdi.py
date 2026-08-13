"""MS Project XML: the three traps, and round-trip fidelity."""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.constraints import ConstraintType
from massingplan.core.model import Calendar, ExchangeActivity, ExchangeSchedule
from massingplan.core.mspdi import (
    MSPDI_LINK_TYPES,
    MSPDIError,
    format_duration_hours,
    mpp_unavailable_reason,
    parse_duration_hours,
    read_mspdi,
    write_mspdi,
)
from massingplan.core.network import ActivityKind, RelationType
from massingplan.core.schedule import schedule


def doc(tasks: str, *, minutes_per_day: int = 480, status: str = "2026-06-01T08:00:00") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Project xmlns="http://schemas.microsoft.com/project">
  <UID>1</UID>
  <Name>Demo</Name>
  <StartDate>2026-06-01T08:00:00</StartDate>
  <StatusDate>{status}</StatusDate>
  <MinutesPerDay>{minutes_per_day}</MinutesPerDay>
  <CalendarUID>1</CalendarUID>
  <Calendars>
    <Calendar>
      <UID>1</UID><Name>Standard</Name>
      <WeekDays>
        <WeekDay><DayType>1</DayType><DayWorking>0</DayWorking></WeekDay>
        <WeekDay><DayType>2</DayType><DayWorking>1</DayWorking></WeekDay>
        <WeekDay><DayType>3</DayType><DayWorking>1</DayWorking></WeekDay>
        <WeekDay><DayType>4</DayType><DayWorking>1</DayWorking></WeekDay>
        <WeekDay><DayType>5</DayType><DayWorking>1</DayWorking></WeekDay>
        <WeekDay><DayType>6</DayType><DayWorking>1</DayWorking></WeekDay>
        <WeekDay><DayType>7</DayType><DayWorking>0</DayWorking></WeekDay>
      </WeekDays>
    </Calendar>
  </Calendars>
  <Tasks>{tasks}</Tasks>
</Project>
"""


def task(uid: str, name: str, hours: int, *, row: int = 1, extra: str = "") -> str:
    return f"""
    <Task>
      <UID>{uid}</UID><ID>{row}</ID><Name>{name}</Name>
      <Duration>PT{hours}H0M0S</Duration>
      <Milestone>0</Milestone><Summary>0</Summary>
      {extra}
    </Task>"""


# -- trap one: ISO-8601 durations ------------------------------------------


def test_pt40h_is_forty_hours_not_forty_days() -> None:
    assert parse_duration_hours("PT40H0M0S") == 40.0
    assert parse_duration_hours("PT7H30M0S") == 7.5
    assert parse_duration_hours("PT0H0M0S") == 0.0


def test_forty_hours_is_five_days_at_eight_and_four_at_ten() -> None:
    """The same document means different durations on different calendars."""
    eight = read_mspdi(doc(task("1", "A", 40), minutes_per_day=480))
    ten = read_mspdi(doc(task("1", "A", 40), minutes_per_day=600))
    assert eight.activities[0].duration_days == 5
    assert ten.activities[0].duration_days == 4


def test_an_unparseable_duration_is_reported_not_guessed() -> None:
    s = read_mspdi(doc("<Task><UID>1</UID><Name>A</Name><Duration>40 days</Duration></Task>"))
    assert s.issues.has("MSPDI.TASK.BAD_DURATION")


def test_duration_formatting_round_trips() -> None:
    for hours in (0, 8, 40, 7.5):
        assert parse_duration_hours(format_duration_hours(hours)) == pytest.approx(hours)


# -- trap two: link types are integers, not alphabetical -------------------


def test_the_link_type_table_is_microsofts_ordering_not_alphabetical() -> None:
    """0=FF, 1=FS, 2=SF, 3=SS. Sorting these "tidily" swaps FF and SS."""
    assert MSPDI_LINK_TYPES == {
        0: RelationType.FF,
        1: RelationType.FS,
        2: RelationType.SF,
        3: RelationType.SS,
    }


def test_type_three_imports_as_ss_not_as_an_alphabetical_guess() -> None:
    tasks = task("1", "A", 40) + task(
        "2",
        "B",
        40,
        row=2,
        extra="<PredecessorLink><PredecessorUID>1</PredecessorUID>"
        "<Type>3</Type><LinkLag>0</LinkLag></PredecessorLink>",
    )
    s = read_mspdi(doc(tasks))
    assert s.relationships[0].type is RelationType.SS


def test_type_zero_imports_as_ff() -> None:
    tasks = task("1", "A", 40) + task(
        "2",
        "B",
        40,
        row=2,
        extra="<PredecessorLink><PredecessorUID>1</PredecessorUID>"
        "<Type>0</Type><LinkLag>0</LinkLag></PredecessorLink>",
    )
    assert read_mspdi(doc(tasks)).relationships[0].type is RelationType.FF


def test_an_unknown_link_type_is_coerced_and_reported() -> None:
    tasks = task("1", "A", 40) + task(
        "2",
        "B",
        40,
        row=2,
        extra="<PredecessorLink><PredecessorUID>1</PredecessorUID>"
        "<Type>9</Type><LinkLag>0</LinkLag></PredecessorLink>",
    )
    s = read_mspdi(doc(tasks))
    assert s.relationships[0].type is RelationType.FS
    assert s.issues.has("MSPDI.LINK.UNKNOWN_TYPE")


# -- trap three: UID is not ID ---------------------------------------------


def test_relationships_follow_uid_not_the_visible_row_number() -> None:
    """ID is reassigned on any reorder; UID is stable and is what links use.

    Here the row numbers are deliberately the reverse of the UIDs, so an
    importer keying on ID would build the relationship backwards.
    """
    tasks = task("100", "First", 40, row=2) + task(
        "200",
        "Second",
        40,
        row=1,
        extra="<PredecessorLink><PredecessorUID>100</PredecessorUID>"
        "<Type>1</Type><LinkLag>0</LinkLag></PredecessorLink>",
    )
    s = read_mspdi(doc(tasks))
    assert [a.id for a in s.activities] == ["100", "200"]
    assert s.relationships[0].predecessor_id == "100"
    assert s.relationships[0].successor_id == "200"


def test_the_project_summary_row_is_not_an_activity() -> None:
    tasks = "<Task><UID>0</UID><ID>0</ID><Name>Project</Name></Task>" + task("1", "A", 40)
    s = read_mspdi(doc(tasks))
    assert [a.id for a in s.activities] == ["1"]


# -- other fields ----------------------------------------------------------


def test_status_date_becomes_the_data_date() -> None:
    s = read_mspdi(doc(task("1", "A", 40), status="2026-06-15T08:00:00"))
    assert s.data_date == date(2026, 6, 15)
    assert s.planned_start == date(2026, 6, 1)


def test_remaining_duration_and_actuals_are_read() -> None:
    s = read_mspdi(
        doc(
            task(
                "1",
                "A",
                80,
                extra="<RemainingDuration>PT24H0M0S</RemainingDuration>"
                "<ActualStart>2026-06-01T08:00:00</ActualStart>",
            )
        )
    )
    activity = s.activities[0]
    assert activity.remaining_duration_days == 3
    assert activity.actual_start == date(2026, 6, 1)


def test_a_calendar_exception_becomes_a_holiday() -> None:
    content = doc(task("1", "A", 40)).replace(
        "</WeekDays>",
        "<WeekDay><DayType>0</DayType><DayWorking>0</DayWorking><TimePeriod>"
        "<FromDate>2026-12-24T08:00:00</FromDate><ToDate>2026-12-26T08:00:00</ToDate>"
        "</TimePeriod></WeekDay></WeekDays>",
    )
    s = read_mspdi(content)
    assert date(2026, 12, 25) in s.calendars[0].holidays
    assert len(s.calendars[0].holidays) == 3


def test_an_absurdly_long_exception_is_dropped_and_reported() -> None:
    content = doc(task("1", "A", 40)).replace(
        "</WeekDays>",
        "<WeekDay><DayType>0</DayType><DayWorking>0</DayWorking><TimePeriod>"
        "<FromDate>2026-01-01T08:00:00</FromDate><ToDate>2030-01-01T08:00:00</ToDate>"
        "</TimePeriod></WeekDay></WeekDays>",
    )
    s = read_mspdi(content)
    assert s.issues.has("MSPDI.CALENDAR.EXCEPTION_TOO_LONG")


def test_a_document_with_no_links_says_so() -> None:
    s = read_mspdi(doc(task("1", "A", 40) + task("2", "B", 40, row=2)))
    assert s.issues.has("MSPDI.LINKS.MISSING")


def test_a_bare_document_without_the_namespace_still_reads() -> None:
    """MS Project writes the namespace; hand-edited files often do not."""
    bare = doc(task("1", "A", 40)).replace(' xmlns="http://schemas.microsoft.com/project"', "")
    s = read_mspdi(bare)
    assert len(s.activities) == 1


def test_malformed_xml_is_refused_by_name() -> None:
    with pytest.raises(MSPDIError, match="not well-formed"):
        read_mspdi("<Project><Tasks>")


# -- round trip ------------------------------------------------------------


def test_a_round_trip_preserves_the_computed_schedule() -> None:
    tasks = (
        task("1", "A", 40)
        + task(
            "2",
            "B",
            80,
            row=2,
            extra="<PredecessorLink><PredecessorUID>1</PredecessorUID>"
            "<Type>1</Type><LinkLag>4800</LinkLag></PredecessorLink>",
        )
        + task(
            "3",
            "C",
            40,
            row=3,
            extra="<PredecessorLink><PredecessorUID>2</PredecessorUID>"
            "<Type>3</Type><LinkLag>0</LinkLag></PredecessorLink>",
        )
    )
    first = read_mspdi(doc(tasks))
    before = schedule(first)
    again = read_mspdi(write_mspdi(before.apply_to(first)))
    after = schedule(again)
    assert after.to_rows() == before.to_rows()


def test_a_round_trip_preserves_the_link_types() -> None:
    tasks = task("1", "A", 40) + task(
        "2",
        "B",
        40,
        row=2,
        extra="<PredecessorLink><PredecessorUID>1</PredecessorUID>"
        "<Type>2</Type><LinkLag>0</LinkLag></PredecessorLink>",
    )
    first = read_mspdi(doc(tasks))
    again = read_mspdi(write_mspdi(first))
    assert again.relationships[0].type is first.relationships[0].type is RelationType.SF


def test_a_lag_survives_the_tenths_of_a_minute_encoding() -> None:
    tasks = task("1", "A", 40) + task(
        "2",
        "B",
        40,
        row=2,
        extra="<PredecessorLink><PredecessorUID>1</PredecessorUID>"
        "<Type>1</Type><LinkLag>9600</LinkLag></PredecessorLink>",
    )
    first = read_mspdi(doc(tasks))
    assert first.relationships[0].lag_days == 2  # 9600 tenths = 16 hours = 2 days
    again = read_mspdi(write_mspdi(first))
    assert again.relationships[0].lag_days == 2


def test_there_is_no_mpp_writer_and_the_reason_is_actionable() -> None:
    reason = mpp_unavailable_reason()
    assert "MSPDI" in reason
    assert "proprietary" in reason


# --- Microsoft's constraint codes -------------------------------------------
#
# Found by round-tripping random schedules and comparing the *computed* dates,
# not the bytes. Every one of these read back with its early dates intact --
# which is exactly why nobody noticed. The damage was all on the late side.


def _pinned(constraint: str, day: str = "2026-06-10") -> ExchangeSchedule:
    """One 5-day task with no predecessors and a constraint, scheduled."""
    start = date(2026, 6, 1)
    return ExchangeSchedule(
        project_id="P",
        project_name="P",
        data_date=start,
        planned_start=start,
        default_calendar_id="CAL5",
        calendars=[
            Calendar(id="CAL5", name="Mon-Fri", working_weekdays=frozenset({0, 1, 2, 3, 4}))
        ],
        activities=[
            ExchangeActivity(
                id="A",
                name="work",
                kind=ActivityKind.TASK,
                calendar_id="CAL5",
                duration_days=5,
                constraint=ConstraintType(constraint),
                constraint_date=date.fromisoformat(day),
            )
        ],
    )


def test_must_start_on_is_read_as_a_pin_and_not_as_a_floor() -> None:
    """Code 2 is Must Start On -- two-sided. Read as a floor it invents float.

    The giveaway is that the *dates* are identical either way. A one-sided
    floor holds the early start exactly where the pin does; it just stops
    holding the late start, so the activity gains slack it does not have and
    reports itself uncritical. Assert on the float, because the dates cannot
    tell the two apart.
    """
    before = _pinned("start_on")
    after = read_mspdi(write_mspdi(before))

    assert after.activities[0].constraint is ConstraintType.START_ON
    assert (
        schedule(after).dates["A"].total_float_days
        == schedule(before).dates["A"].total_float_days
        == 0
    )


def test_must_finish_on_does_not_become_finish_no_earlier_than() -> None:
    """Codes 3 and 6 are different constraints; the table mapped both to 6.

    No MSPDI file, however written, could produce a FINISH_ON.
    """
    assert read_mspdi(write_mspdi(_pinned("finish_on"))).activities[0].constraint is (
        ConstraintType.FINISH_ON
    )
    assert read_mspdi(write_mspdi(_pinned("finish_on_or_after"))).activities[0].constraint is (
        ConstraintType.FINISH_ON_OR_AFTER
    )


def test_every_microsoft_constraint_code_round_trips_to_itself() -> None:
    """The whole table both ways, so a future tidy-up cannot re-pair them."""
    for value in (
        "start_on",
        "finish_on",
        "start_on_or_after",
        "start_on_or_before",
        "finish_on_or_after",
        "finish_on_or_before",
    ):
        back = read_mspdi(write_mspdi(_pinned(value))).activities[0].constraint
        assert back is ConstraintType(value), f"{value} came back as {back.value}"


def test_a_mandatory_constraint_is_written_as_a_pin_and_the_loss_is_reported() -> None:
    """MS Project has nothing that overrides logic, so this one cannot survive.

    It degrades to the two-sided pin rather than the one-sided floor: the
    conflict then still shows up as negative float instead of vanishing.
    """
    source = _pinned("mandatory_start")
    back = read_mspdi(write_mspdi(source))

    assert source.issues.has("MSPDI.TASK.MANDATORY_DOWNGRADED")
    assert back.activities[0].constraint is ConstraintType.START_ON
    assert back.activities[0].constraint_date == date(2026, 6, 10)


def test_a_milestone_reads_back_at_the_date_ms_project_shows_it() -> None:
    """`<Milestone>` is one boolean for both milestone kinds.

    Read as a *finish* milestone it is snapped back to the previous working
    day -- a date the file itself does not contain and MS Project never
    displays. A start milestone shows where the file says.
    """
    source = _pinned("none")
    source.activities[0].kind = ActivityKind.START_MILESTONE
    source.activities[0].duration_days = 0

    back = read_mspdi(write_mspdi(source))
    assert back.activities[0].kind is ActivityKind.START_MILESTONE
    assert schedule(back).dates["A"].start == schedule(source).dates["A"].start
