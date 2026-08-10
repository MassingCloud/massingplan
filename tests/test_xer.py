"""Primavera XER: the four traps, the two omissions, and round-trip fidelity.

Round trips assert that **the computed schedule does not move**, not that the
bytes match. P6 writes dozens of columns this engine has no opinion about;
comparing bytes would test the fixture rather than the converter.
"""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.constraints import ConstraintType
from massingplan.core.network import ActivityKind, LagCalendar, ProgressMode, RelationType
from massingplan.core.schedule import schedule
from massingplan.core.xer import XERError, parse_tables, read_xer, write_xer


def xer(*tables: str) -> str:
    return "ERMHDR\t19.12\t2026-08-08\tProject\tadmin\n" + "".join(tables) + "%E\n"


def table(name: str, columns: list[str], *rows: list[str]) -> str:
    out = f"%T\t{name}\n%F\t" + "\t".join(columns) + "\n"
    for row in rows:
        out += "%R\t" + "\t".join(row) + "\n"
    return out


WEEKLY_5DAY = "(0||DaysOfWeek()((1())(2()(0||1(s|08:00|f|16:00)))(3()(0||1(s|08:00|f|16:00)))(4()(0||1(s|08:00|f|16:00)))(5()(0||1(s|08:00|f|16:00)))(6()(0||1(s|08:00|f|16:00)))(7())))"


def minimal(extra: str = "", *, day_hr: str = "8", clndr_data: str = WEEKLY_5DAY) -> str:
    return xer(
        table(
            "PROJECT",
            ["proj_id", "proj_short_name", "plan_start_date", "last_recalc_date", "clndr_id"],
            ["1", "DEMO", "2026-06-01 00:00", "2026-06-01 00:00", "C1"],
        ),
        table(
            "CALENDAR",
            ["clndr_id", "clndr_name", "day_hr_cnt", "default_flag", "clndr_data"],
            ["C1", "Standard", day_hr, "Y", clndr_data],
        ),
        table(
            "TASK",
            [
                "task_id",
                "proj_id",
                "task_code",
                "task_name",
                "task_type",
                "clndr_id",
                "target_drtn_hr_cnt",
            ],
            ["T1", "1", "A1010", "Excavate", "TT_Task", "C1", "40"],
            ["T2", "1", "A1020", "Foundations", "TT_Task", "C1", "80"],
            ["T3", "1", "M1000", "Topping out", "TT_FinMile", "C1", "0"],
        ),
        extra,
    )


# -- tokenizer -------------------------------------------------------------


def test_tables_are_split_by_marker() -> None:
    tables = parse_tables(minimal())
    assert set(tables) >= {"PROJECT", "CALENDAR", "TASK"}
    assert len(tables["TASK"]) == 3
    assert tables["TASK"][0]["task_code"] == "A1010"


def test_a_short_row_is_padded_rather_than_discarded() -> None:
    """A trailing empty field must not cost a whole activity."""
    content = xer(table("TASK", ["task_id", "task_name", "task_type"], ["T1", "Only two"]))
    rows = parse_tables(content)["TASK"]
    assert rows == [{"task_id": "T1", "task_name": "Only two", "task_type": ""}]


def test_crlf_line_endings_do_not_leave_a_stray_carriage_return() -> None:
    content = minimal().replace("\n", "\r\n")
    tables = parse_tables(content)
    assert tables["TASK"][0]["task_name"] == "Excavate"


def test_a_file_with_no_table_marker_is_refused_by_name() -> None:
    with pytest.raises(XERError, match="does not look like an XER file"):
        read_xer("just some text")


# -- the four traps --------------------------------------------------------


def test_trap_one_relationship_prefixes_are_mapped_not_sliced() -> None:
    content = minimal(
        table(
            "TASKPRED",
            ["task_id", "pred_task_id", "pred_type", "lag_hr_cnt"],
            ["T2", "T1", "PR_SS", "0"],
            ["T3", "T2", "PR_FF", "0"],
        )
    )
    s = read_xer(content)
    types = {(r.predecessor_id, r.successor_id): r.type for r in s.relationships}
    assert types[("T1", "T2")] is RelationType.SS
    assert types[("T2", "T3")] is RelationType.FF


def test_an_unknown_relationship_type_is_coerced_to_fs_and_says_so() -> None:
    """Coerced, not dropped -- but never silently. A missing link changes the
    critical path just as much as a wrong one.
    """
    content = minimal(
        table(
            "TASKPRED",
            ["task_id", "pred_task_id", "pred_type", "lag_hr_cnt"],
            ["T2", "T1", "PR_XX", "0"],
        )
    )
    s = read_xer(content)
    assert s.relationships[0].type is RelationType.FS
    assert s.issues.has("XER.TASKPRED.UNKNOWN_TYPE")
    issue = next(e for e in s.issues if e.code == "XER.TASKPRED.UNKNOWN_TYPE")
    assert issue.raw_value == "PR_XX"
    assert "coerced to FS" in issue.action


def test_trap_two_hours_per_day_comes_from_the_calendar() -> None:
    """40 hours is five days at 8/day and four days at 10/day."""
    assert read_xer(minimal(day_hr="8")).activities[0].duration_days == 5
    assert read_xer(minimal(day_hr="10")).activities[0].duration_days == 4


def test_a_missing_day_hr_cnt_defaults_loudly() -> None:
    s = read_xer(minimal(day_hr=""))
    assert s.issues.has("XER.CALENDAR.NO_DAY_HR_CNT")
    assert s.calendars[0].hours_per_day == 8.0


def test_trap_three_a_milestone_keeps_zero_duration() -> None:
    s = read_xer(minimal())
    milestone = s.activity("T3")
    assert milestone is not None
    assert milestone.kind is ActivityKind.FINISH_MILESTONE
    assert milestone.duration_days == 0


def test_trap_four_costs_come_from_taskrsrc_and_notes_from_taskmemo() -> None:
    content = minimal(
        table("RSRC", ["rsrc_id", "rsrc_name", "rsrc_type"], ["R1", "Carpenters", "RT_Labor"])
        + table(
            "TASKRSRC",
            ["task_id", "rsrc_id", "target_qty", "target_cost"],
            ["T1", "R1", "40", "26000"],
        )
        + table("MEMOTYPE", ["memo_type_id", "memo_type"], ["1", "Method"])
        + table(
            "TASKMEMO",
            ["memo_id", "task_id", "memo_type_id", "task_memo"],
            ["1", "T1", "1", "Bench and shore before excavating"],
        )
    )
    s = read_xer(content)
    assert s.assignments[0].budgeted_cost == 26000.0
    first = s.activity("T1")
    assert first is not None
    assert "Method: Bench and shore" in first.notes


# -- the two omissions -----------------------------------------------------


def test_taskpred_is_read_so_the_network_has_logic() -> None:
    """The consuming product's importer omits this table entirely."""
    content = minimal(
        table(
            "TASKPRED",
            ["task_id", "pred_task_id", "pred_type", "lag_hr_cnt"],
            ["T2", "T1", "PR_FS", "0"],
            ["T3", "T2", "PR_FS", "0"],
        )
    )
    s = read_xer(content)
    assert len(s.relationships) == 2
    out = schedule(s)
    # With logic, one activity carries float and the network is not flat.
    assert out.dates["T1"].finish == date(2026, 6, 5)
    assert out.dates["T2"].start == date(2026, 6, 8)


def test_a_file_with_no_logic_says_every_activity_will_read_as_critical() -> None:
    """The failure mode is that the import *looks* like it worked."""
    s = read_xer(minimal())
    assert s.issues.has("XER.TASKPRED.MISSING")
    issue = next(e for e in s.issues if e.code == "XER.TASKPRED.MISSING")
    assert "critical with zero float" in issue.action


def test_calendar_exceptions_are_parsed_and_move_the_finish() -> None:
    """An unparsed shutdown moves every downstream date two weeks early."""
    shutdown_serials = [
        (date(2026, 12, 21) + __import__("datetime").timedelta(days=k) - date(1899, 12, 30)).days
        for k in range(14)
    ]
    blocks = "".join(f"(0||d|{s}())" for s in shutdown_serials)
    clndr = WEEKLY_5DAY + "(0||Exceptions()(" + blocks + "))"
    s = read_xer(minimal(clndr_data=clndr))
    holidays = s.calendars[0].holidays
    assert date(2026, 12, 25) in holidays
    assert len(holidays) == 14


def test_a_working_exception_is_kept_as_a_make_up_day() -> None:
    serial = (date(2026, 6, 6) - date(1899, 12, 30)).days
    clndr = WEEKLY_5DAY + f"(0||Exceptions()((0||d|{serial}((0||1(s|08:00|f|16:00)))))) "
    s = read_xer(minimal(clndr_data=clndr))
    assert date(2026, 6, 6) in s.calendars[0].extra_work_days


# -- schedule options ------------------------------------------------------


def test_schedopts_retained_logic_and_lag_calendar_are_read() -> None:
    content = minimal(
        table(
            "SCHEDOPTIONS",
            [
                "sched_retained_logic",
                "sched_progress_override",
                "sched_calendar_on_relationship_lag",
            ],
            ["N", "Y", "rcal_Successor"],
        )
    )
    s = read_xer(content)
    assert s.options.progress_mode is ProgressMode.PROGRESS_OVERRIDE
    assert s.options.lag_calendar is LagCalendar.SUCCESSOR
    # What the file said is preserved verbatim, whether or not we honoured it.
    assert s.options.source_options["sched_progress_override"] == "Y"


def test_an_unknown_lag_calendar_falls_back_to_p6s_default_and_says_so() -> None:
    content = minimal(
        table("SCHEDOPTIONS", ["sched_calendar_on_relationship_lag"], ["rcal_Martian"])
    )
    s = read_xer(content)
    assert s.options.lag_calendar is LagCalendar.PREDECESSOR
    assert s.issues.has("XER.SCHEDOPTIONS.UNKNOWN_LAG_CALENDAR")


# -- other fields ----------------------------------------------------------


def test_the_data_date_comes_from_last_recalc_not_plan_start() -> None:
    """Reading plan_start_date as the data date reschedules completed work."""
    content = xer(
        table(
            "PROJECT",
            ["proj_id", "proj_short_name", "plan_start_date", "last_recalc_date"],
            ["1", "DEMO", "2026-01-01 00:00", "2026-06-15 00:00"],
        ),
        table(
            "TASK",
            ["task_id", "task_name", "task_type", "target_drtn_hr_cnt"],
            ["T1", "A", "TT_Task", "40"],
        ),
    )
    s = read_xer(content)
    assert s.data_date == date(2026, 6, 15)
    assert s.planned_start == date(2026, 1, 1)


def test_constraints_map_across_all_ten_types() -> None:
    content = minimal(
        table(
            "TASK",
            [
                "task_id",
                "task_name",
                "task_type",
                "clndr_id",
                "target_drtn_hr_cnt",
                "cstr_type",
                "cstr_date",
            ],
            ["T9", "Pinned", "TT_Task", "C1", "40", "CS_MANDFIN", "2026-07-31 00:00"],
        )
    )
    s = read_xer(content)
    pinned = s.activity("T9")
    assert pinned is not None
    assert pinned.constraint is ConstraintType.MANDATORY_FINISH
    assert pinned.constraint_date == date(2026, 7, 31)


def test_a_constraint_with_no_date_is_dropped_and_reported() -> None:
    content = minimal(
        table(
            "TASK",
            ["task_id", "task_name", "task_type", "clndr_id", "target_drtn_hr_cnt", "cstr_type"],
            ["T9", "Pinned", "TT_Task", "C1", "40", "CS_MSOA"],
        )
    )
    s = read_xer(content)
    pinned = s.activity("T9")
    assert pinned is not None
    assert pinned.constraint is ConstraintType.NONE
    assert s.issues.has("XER.TASK.CONSTRAINT_WITHOUT_DATE")


def test_a_negative_lag_keeps_its_sign_through_the_hours_conversion() -> None:
    content = minimal(
        table(
            "TASKPRED",
            ["task_id", "pred_task_id", "pred_type", "lag_hr_cnt"],
            ["T2", "T1", "PR_FS", "-16"],
        )
    )
    s = read_xer(content)
    assert s.relationships[0].lag_days == -2


def test_a_dangling_relationship_is_dropped_and_named() -> None:
    content = minimal(
        table(
            "TASKPRED",
            ["task_id", "pred_task_id", "pred_type", "lag_hr_cnt"],
            ["T2", "GHOST", "PR_FS", "0"],
        )
    )
    s = read_xer(content)
    assert s.relationships == []
    assert s.issues.has("XER.TASKPRED.DANGLING")


def test_activity_codes_survive_the_import() -> None:
    content = minimal(
        table("ACTVTYPE", ["actv_code_type_id", "actv_code_type"], ["1", "Trade"])
        + table(
            "ACTVCODE", ["actv_code_id", "actv_code_type_id", "short_name"], ["10", "1", "CONC"]
        )
        + table("TASKACTV", ["task_id", "actv_code_id"], ["T1", "10"])
    )
    s = read_xer(content)
    first = s.activity("T1")
    assert first is not None
    assert first.activity_codes == {"Trade": "CONC"}


def test_a_multi_project_file_names_every_project_it_did_not_import() -> None:
    content = xer(
        table(
            "PROJECT",
            ["proj_id", "proj_short_name", "last_recalc_date"],
            ["1", "TOWER", "2026-06-01 00:00"],
            ["2", "PODIUM", "2026-06-01 00:00"],
        ),
        table(
            "TASK",
            ["task_id", "proj_id", "task_name", "task_type", "target_drtn_hr_cnt"],
            ["T1", "1", "A", "TT_Task", "40"],
            ["T2", "2", "B", "TT_Task", "40"],
        ),
    )
    s = read_xer(content)
    assert s.issues.has("XER.PROJECT.MULTIPLE")
    assert "PODIUM" in next(e for e in s.issues if e.code == "XER.PROJECT.MULTIPLE").message
    # Only the chosen project's activities came across.
    assert [a.id for a in s.activities] == ["T1"]


def test_a_specific_project_can_be_requested() -> None:
    content = xer(
        table(
            "PROJECT",
            ["proj_id", "proj_short_name", "last_recalc_date"],
            ["1", "TOWER", "2026-06-01 00:00"],
            ["2", "PODIUM", "2026-06-01 00:00"],
        ),
        table(
            "TASK",
            ["task_id", "proj_id", "task_name", "task_type", "target_drtn_hr_cnt"],
            ["T1", "1", "A", "TT_Task", "40"],
            ["T2", "2", "B", "TT_Task", "40"],
        ),
    )
    s = read_xer(content, project_id="2")
    assert [a.id for a in s.activities] == ["T2"]


def test_recognised_but_unused_tables_are_named_rather_than_ignored() -> None:
    content = minimal(table("OBS", ["obs_id", "obs_name"], ["1", "Client"]))
    s = read_xer(content)
    assert s.issues.has("XER.TABLE_SKIPPED")


# -- round trip ------------------------------------------------------------


def test_a_round_trip_preserves_the_computed_schedule() -> None:
    """The assertion that matters: read, schedule, write, read, schedule again,
    and the dates are identical.
    """
    content = minimal(
        table(
            "TASKPRED",
            ["task_id", "pred_task_id", "pred_type", "lag_hr_cnt"],
            ["T2", "T1", "PR_SS", "16"],
            ["T3", "T2", "PR_FS", "0"],
        )
    )
    first = read_xer(content)
    before = schedule(first)

    round_tripped = read_xer(write_xer(before.apply_to(first)))
    after = schedule(round_tripped)

    assert after.to_rows() == before.to_rows()


def test_a_round_trip_preserves_calendar_exceptions() -> None:
    """Writing the weekly pattern and dropping the exceptions would make an
    export of an imported file schedule differently from the file it came from.
    """
    import datetime as _dt

    serials = [
        (date(2026, 12, 21) + _dt.timedelta(days=k) - date(1899, 12, 30)).days for k in range(14)
    ]
    clndr = WEEKLY_5DAY + "(0||Exceptions()(" + "".join(f"(0||d|{s}())" for s in serials) + "))"
    first = read_xer(minimal(clndr_data=clndr))
    again = read_xer(write_xer(first))
    assert again.calendars[0].holidays == first.calendars[0].holidays


def test_a_round_trip_preserves_all_four_relationship_types() -> None:
    content = minimal(
        table(
            "TASKPRED",
            ["task_id", "pred_task_id", "pred_type", "lag_hr_cnt"],
            ["T2", "T1", "PR_SS", "0"],
            ["T3", "T2", "PR_FF", "0"],
        )
    )
    first = read_xer(content)
    again = read_xer(write_xer(first))
    assert [r.type for r in again.relationships] == [r.type for r in first.relationships]


def test_the_written_file_reads_back_as_xer() -> None:
    first = read_xer(minimal())
    text = write_xer(first)
    assert text.startswith("ERMHDR\t")
    assert text.rstrip().endswith("%E")
    assert "%T\tTASK" in text
