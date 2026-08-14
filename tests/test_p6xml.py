"""Primavera P6 XML, and the three traps that make it not-quite-MSPDI.

The fixture below is shaped after real P6 exports: the element names, the
enumeration spellings, the hours-not-days durations and the fraction-not-
percentage progress were all read out of genuine files rather than recalled.
"""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.constraints import ConstraintType
from massingplan.core.network import ActivityKind, LagCalendar, ProgressMode, RelationType
from massingplan.core.p6xml import P6XMLError, read_p6xml, read_p6xml_all, write_p6xml
from massingplan.core.schedule import schedule


def doc(activities: str, *, projects: str = "", namespaced: bool = False) -> str:
    ns = (
        ' xmlns="http://xmlns.oracle.com/Primavera/P6/V18.1/API/BusinessObjects"'
        if namespaced
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<APIBusinessObjects{ns}>
  <ProjectList>
    <Project ObjectId="34136"><Id>DEMO</Id><Name>Demo</Name></Project>
  </ProjectList>
  <Calendar>
    <ObjectId>12002</ObjectId>
    <Name>Mon-Fri</Name>
    <Type>Global</Type>
    <HoursPerDay>8.0</HoursPerDay>
    <StandardWorkWeek>
      <StandardWorkHours><DayOfWeek>Monday</DayOfWeek>
        <WorkTime><Start>08:00:00</Start><Finish>17:00:00</Finish></WorkTime></StandardWorkHours>
      <StandardWorkHours><DayOfWeek>Tuesday</DayOfWeek>
        <WorkTime><Start>08:00:00</Start><Finish>17:00:00</Finish></WorkTime></StandardWorkHours>
      <StandardWorkHours><DayOfWeek>Wednesday</DayOfWeek>
        <WorkTime><Start>08:00:00</Start><Finish>17:00:00</Finish></WorkTime></StandardWorkHours>
      <StandardWorkHours><DayOfWeek>Thursday</DayOfWeek>
        <WorkTime><Start>08:00:00</Start><Finish>17:00:00</Finish></WorkTime></StandardWorkHours>
      <StandardWorkHours><DayOfWeek>Friday</DayOfWeek>
        <WorkTime><Start>08:00:00</Start><Finish>17:00:00</Finish></WorkTime></StandardWorkHours>
      <StandardWorkHours><DayOfWeek>Saturday</DayOfWeek></StandardWorkHours>
      <StandardWorkHours><DayOfWeek>Sunday</DayOfWeek></StandardWorkHours>
    </StandardWorkWeek>
    <HolidayOrExceptions></HolidayOrExceptions>
  </Calendar>
  <Project>
    <ObjectId>34136</ObjectId>
    <Id>DEMO</Id>
    <Name>Demo</Name>
    <DataDate>2026-06-01T00:00:00</DataDate>
    <PlannedStartDate>2026-06-01T08:00:00</PlannedStartDate>
    <OutOfSequenceScheduleType>Retained Logic</OutOfSequenceScheduleType>
    <RelationshipLagCalendar>Predecessor Activity Calendar</RelationshipLagCalendar>
    <MakeOpenEndedActivitiesCritical>0</MakeOpenEndedActivitiesCritical>
    <ActivityDefaultCalendarObjectId>12002</ActivityDefaultCalendarObjectId>
{activities}
  </Project>
{projects}
</APIBusinessObjects>
"""


def activity(
    object_id: str,
    code: str,
    hours: float,
    *,
    kind: str = "Task Dependent",
    extra: str = "",
) -> str:
    return f"""    <Activity>
      <ObjectId>{object_id}</ObjectId>
      <Id>{code}</Id>
      <Name>Activity {code}</Name>
      <Type>{kind}</Type>
      <CalendarObjectId>12002</CalendarObjectId>
      <PlannedDuration>{hours}</PlannedDuration>
      <PrimaryConstraintType/>
{extra}
    </Activity>"""


def link(pred: str, succ: str, kind: str = "Finish to Start", lag: float = 0.0) -> str:
    return f"""    <Relationship>
      <PredecessorActivityObjectId>{pred}</PredecessorActivityObjectId>
      <SuccessorActivityObjectId>{succ}</SuccessorActivityObjectId>
      <Type>{kind}</Type>
      <Lag>{lag}</Lag>
    </Relationship>"""


# -- the three traps -------------------------------------------------------


def test_durations_are_hours_not_days() -> None:
    """`<PlannedDuration>360.0` is forty-five days at eight hours a day.

    Read as days it is a fourteen-month activity, and the schedule still
    computes -- which is what makes the unit the trap rather than the error.
    """
    s = read_p6xml(doc(activity("1", "A1000", 360.0)))
    assert s.activities[0].duration_days == 45


def test_lag_is_hours_and_a_lead_stays_negative() -> None:
    """`<Lag>-32` is a four-day lead. Its sign is the whole of its meaning."""
    s = read_p6xml(
        doc(
            activity("1", "A", 40.0)
            + "\n"
            + activity("2", "B", 40.0)
            + "\n"
            + link("1", "2", lag=-32.0)
        )
    )
    assert len(s.relationships) == 1
    assert s.relationships[0].lag_days == -4


def test_percent_complete_is_a_fraction_not_a_percentage() -> None:
    """A completed P6 activity carries `1.0`, where MSPDI carries `100`.

    Applying the MSPDI rule here reports a finished activity as one percent
    done -- and the project as barely started, on a job that is finished.
    """
    s = read_p6xml(
        doc(
            activity(
                "1",
                "A",
                40.0,
                extra=(
                    "      <PercentComplete>1.0</PercentComplete>\n"
                    "      <DurationPercentComplete>1.0</DurationPercentComplete>"
                ),
            )
        )
    )
    assert s.activities[0].percent_complete == 100.0


def test_type_is_read_from_its_parent_and_not_searched_for() -> None:
    """`<Type>` means four different things in this format.

    The calendar in the fixture is `Global` and the relationship is `Finish to
    Start`; a document-wide search for `Type` would find the calendar's first
    and read every activity as a task called Global.
    """
    s = read_p6xml(
        doc(
            activity("1", "A", 40.0)
            + "\n"
            + activity("2", "PC", 0.0, kind="Finish Milestone")
            + "\n"
            + link("1", "2")
        )
    )
    kinds = {a.id: a.kind for a in s.activities}
    assert kinds["A"] is ActivityKind.TASK
    assert kinds["PC"] is ActivityKind.FINISH_MILESTONE
    assert s.relationships[0].type is RelationType.FS


# -- namespaces ------------------------------------------------------------


@pytest.mark.parametrize("namespaced", [False, True])
def test_both_namespace_forms_read_identically(namespaced: bool) -> None:
    """Some exports declare a default xmlns and some do not. Both are real.

    Matching a fixed namespace reads one and finds no activities in the other
    -- an empty schedule, which imports without an error and analyses to
    nothing.
    """
    s = read_p6xml(doc(activity("1", "A", 40.0), namespaced=namespaced))
    assert [a.id for a in s.activities] == ["A"]
    assert s.project_id == "DEMO"


# -- calendars -------------------------------------------------------------


def test_a_day_with_no_work_time_is_a_non_working_day() -> None:
    """P6 writes a weekend as a present-but-empty `<StandardWorkHours>`.

    Presence cannot be the test: every day of the week is present.
    """
    s = read_p6xml(doc(activity("1", "A", 40.0)))
    assert s.calendars[0].working_weekdays == {0, 1, 2, 3, 4}


def test_a_holiday_and_a_make_up_day_are_told_apart() -> None:
    """Both are exceptions; only one subtracts."""
    content = doc(activity("1", "A", 40.0)).replace(
        "<HolidayOrExceptions></HolidayOrExceptions>",
        "<HolidayOrExceptions>"
        "<HolidayOrException><Date>2026-12-25T00:00:00</Date></HolidayOrException>"
        "<HolidayOrException><Date>2026-06-06T00:00:00</Date>"
        "<WorkTime><Start>08:00:00</Start><Finish>17:00:00</Finish></WorkTime>"
        "</HolidayOrException>"
        "</HolidayOrExceptions>",
    )
    calendar = read_p6xml(content).calendars[0]
    assert date(2026, 12, 25) in calendar.holidays
    assert date(2026, 6, 6) in calendar.extra_work_days


# -- scheduling options ----------------------------------------------------


def test_the_projects_own_scheduling_options_are_honoured() -> None:
    """Taking the defaults computes a different schedule from the one the
    file's author saw, and says nothing about it."""
    content = (
        doc(activity("1", "A", 40.0))
        .replace(
            "<OutOfSequenceScheduleType>Retained Logic</OutOfSequenceScheduleType>",
            "<OutOfSequenceScheduleType>Progress Override</OutOfSequenceScheduleType>",
        )
        .replace(
            "<RelationshipLagCalendar>Predecessor Activity Calendar</RelationshipLagCalendar>",
            "<RelationshipLagCalendar>Successor Activity Calendar</RelationshipLagCalendar>",
        )
        .replace(
            "<MakeOpenEndedActivitiesCritical>0</MakeOpenEndedActivitiesCritical>",
            "<MakeOpenEndedActivitiesCritical>1</MakeOpenEndedActivitiesCritical>",
        )
    )
    s = read_p6xml(content)
    assert s.options.progress_mode is ProgressMode.PROGRESS_OVERRIDE
    assert s.options.lag_calendar is LagCalendar.SUCCESSOR
    assert s.options.open_ends_are_critical is True


# -- the reason this module exists: a series in one file -------------------


def test_a_file_with_baselines_yields_every_project() -> None:
    """The thing XER cannot do, and the argument for the whole module.

    Windows analysis needs a series of dated schedules. Here they arrive as one
    document instead of four separate exports whose data dates somebody had to
    keep straight by hand.
    """
    baseline = (
        """  <Project>
    <ObjectId>99</ObjectId>
    <Id>DEMO-B1</Id>
    <Name>Demo Baseline</Name>
    <DataDate>2026-05-01T00:00:00</DataDate>
"""
        + activity("91", "A", 40.0)
        + """
  </Project>"""
    )
    schedules = read_p6xml_all(doc(activity("1", "A", 80.0), projects=baseline))

    assert [s.project_id for s in schedules] == ["DEMO", "DEMO-B1"]
    assert schedules[0].data_date == date(2026, 6, 1)
    assert schedules[1].data_date == date(2026, 5, 1)
    assert schedules[0].activities[0].duration_days == 10
    assert schedules[1].activities[0].duration_days == 5


def test_reading_one_project_from_a_multi_project_file_says_so() -> None:
    """Silently reading one of four is how somebody analyses the wrong schedule."""
    baseline = (
        "  <Project><Id>DEMO-B1</Id><Name>B</Name>" + activity("91", "A", 40.0) + "</Project>"
    )
    s = read_p6xml(doc(activity("1", "A", 80.0), projects=baseline))
    assert s.project_id == "DEMO"
    assert s.issues.has("P6XML.MULTIPLE_PROJECTS")


def test_a_named_project_can_be_asked_for_and_a_missing_one_refuses() -> None:
    baseline = (
        "  <Project><Id>DEMO-B1</Id><Name>B</Name>" + activity("91", "A", 40.0) + "</Project>"
    )
    content = doc(activity("1", "A", 80.0), projects=baseline)
    assert read_p6xml(content, project_id="DEMO-B1").project_id == "DEMO-B1"
    with pytest.raises(P6XMLError, match="no project"):
        read_p6xml(content, project_id="NOPE")


# -- what it refuses -------------------------------------------------------


def test_a_project_list_stub_is_not_mistaken_for_a_project() -> None:
    """`<ProjectList>` is an index of the file, not a schedule.

    Reading one as a project yields an empty schedule that imports without an
    error -- the failure mode this whole codebase treats as the expensive one.
    """
    empty = """<?xml version="1.0"?>
<APIBusinessObjects>
  <ProjectList><Project><Id>DEMO</Id><Name>Demo</Name></Project></ProjectList>
</APIBusinessObjects>"""
    with pytest.raises(P6XMLError, match="no project with activities"):
        read_p6xml(empty)


def test_a_top_level_project_with_no_activities_is_not_a_project_either() -> None:
    """The other empty, guarded by a different rule from the ProjectList one.

    A `<ProjectList>` stub is excluded because only direct children of the root
    are searched. This is a real top-level `<Project>` that simply has nothing
    in it -- and reading it yields a schedule that imports cleanly and analyses
    to nothing.
    """
    empty = """<?xml version="1.0"?>
<APIBusinessObjects>
  <Project><ObjectId>1</ObjectId><Id>DEMO</Id><Name>Demo</Name></Project>
</APIBusinessObjects>"""
    with pytest.raises(P6XMLError, match="no project with activities"):
        read_p6xml(empty)


def test_a_project_list_stub_is_excluded_even_beside_a_real_project() -> None:
    """The stub carries the same `<Id>` as the project it indexes.

    Two rules exclude it and only one is load-bearing, which is worth being
    accurate about: `_children` never descends into `<ProjectList>`, *and* the
    stub has no activities. Removing the first alone changes nothing -- the
    activity guard still catches it -- so this asserts the outcome rather than
    a mechanism. A first draft of this docstring claimed the descendant search
    would let the empty one win; sabotaging it proved otherwise.
    """
    s = read_p6xml(doc(activity("1", "A", 40.0)))
    assert len(s.activities) == 1
    assert not s.issues.has("P6XML.MULTIPLE_PROJECTS"), (
        "the ProjectList stub was counted as a second project"
    )


def test_an_mspdi_document_is_refused_rather_than_half_read() -> None:
    mspdi = '<?xml version="1.0"?><Project xmlns="http://schemas.microsoft.com/project"/>'
    with pytest.raises(P6XMLError, match="APIBusinessObjects"):
        read_p6xml(mspdi)


def test_a_broken_document_names_the_problem() -> None:
    with pytest.raises(P6XMLError, match="well-formed"):
        read_p6xml("<APIBusinessObjects><Project>")


def test_an_external_relationship_is_reported_not_dropped_silently() -> None:
    """The other end is in another project's file. Dropping the link removes a
    constraint, and dates then compute *earlier* with nothing to show for it."""
    content = doc(activity("1", "A", 40.0) + "\n" + link("999", "1"))
    s = read_p6xml(content)
    assert s.relationships == []
    assert s.issues.has("P6XML.RELATIONSHIP.EXTERNAL")


def test_an_unknown_relationship_type_imports_as_finish_to_start_and_says_so() -> None:
    content = doc(
        activity("1", "A", 40.0) + "\n" + activity("2", "B", 40.0) + "\n" + link("1", "2", "Wobble")
    )
    s = read_p6xml(content)
    assert s.relationships[0].type is RelationType.FS
    assert s.issues.has("P6XML.RELATIONSHIP.UNKNOWN_TYPE")


def test_a_constraint_without_a_date_is_dropped_rather_than_guessed() -> None:
    content = doc(
        activity(
            "1",
            "A",
            40.0,
            extra="      <PrimaryConstraintType>Start On or After</PrimaryConstraintType>",
        ).replace("      <PrimaryConstraintType/>\n", "")
    )
    s = read_p6xml(content)
    assert s.activities[0].constraint is ConstraintType.NONE
    assert s.issues.has("P6XML.ACTIVITY.CONSTRAINT_WITHOUT_DATE")


# -- round trip ------------------------------------------------------------


def test_read_schedule_write_read_preserves_the_computed_dates() -> None:
    """The acceptance criterion from the roadmap, and the only one that matters:
    not that the bytes match, but that the answer does."""
    content = doc(
        "\n".join(
            [
                activity("1", "A", 40.0),
                activity("2", "B", 24.0),
                activity(
                    "3",
                    "C",
                    16.0,
                    extra=(
                        "      <PrimaryConstraintType>Start On or After</PrimaryConstraintType>\n"
                        "      <PrimaryConstraintDate>2026-07-01T08:00:00</PrimaryConstraintDate>"
                    ),
                ).replace("      <PrimaryConstraintType/>\n", ""),
                activity("4", "PC", 0.0, kind="Finish Milestone"),
                link("1", "2"),
                link("2", "3", "Start to Start", 16.0),
                link("3", "4"),
            ]
        )
    )
    original = read_p6xml(content)
    reread = read_p6xml(write_p6xml(original))

    before = {r["activity_id"]: r for r in schedule(original).to_rows()}
    after = {r["activity_id"]: r for r in schedule(reread).to_rows()}
    assert set(before) == set(after)
    for aid in before:
        for key in ("start", "finish", "total_float_days", "is_critical"):
            assert before[aid][key] == after[aid][key], f"{aid}.{key} moved"

    assert reread.activities[2].constraint is ConstraintType.START_ON_OR_AFTER
    assert reread.activities[2].constraint_date == date(2026, 7, 1)
    assert reread.activities[3].kind is ActivityKind.FINISH_MILESTONE
    assert reread.relationships[1].type is RelationType.SS
    assert reread.relationships[1].lag_days == 2
    assert reread.options.progress_mode is original.options.progress_mode
    assert reread.options.lag_calendar is original.options.lag_calendar


def test_an_upload_is_told_apart_from_mspdi_by_its_root_element() -> None:
    """Both formats are `.xml`, so the extension cannot decide.

    Reading a P6 export with the MSPDI reader finds no `<Task>` elements and
    returns an empty schedule -- an import that succeeds and contains nothing.
    """
    from massingplan.api.schedules import import_file

    content = doc(
        activity("1", "A", 40.0) + "\n" + activity("2", "B", 24.0) + "\n" + link("1", "2")
    )
    result = import_file(content, filename="export.xml")
    assert result["source"]["source_format"] == "p6xml"
    assert result["source"]["activities"] == 2
    assert result["has_logic"] is True
    assert len(result["activities"]) == 2

    mspdi = """<?xml version="1.0"?>
<Project xmlns="http://schemas.microsoft.com/project">
  <StartDate>2026-06-01T08:00:00</StartDate><MinutesPerDay>480</MinutesPerDay>
  <Tasks><Task><UID>1</UID><ID>1</ID><Name>A</Name><Duration>PT40H0M0S</Duration>
  <Start>2026-06-01T08:00:00</Start><Finish>2026-06-05T17:00:00</Finish></Task></Tasks>
</Project>"""
    other = import_file(mspdi, filename="other.xml")["source"]
    assert other["source_format"] == "mspdi"
    assert other["activities"] == 1
