"""The vendored massingplan engine, behind massing's own interfaces.

Two things are defended here:

1. **`schedule_cpm.compute()` still returns what every caller reads.** EVM, the
   extension-of-time analysis, resource loading, the vitals dashboard and the
   Gantt renderer all consume that dict. New keys are fine; a missing or renamed
   one is a production break.
2. **A real P6 file imports with its logic.** This is the acceptance criterion
   for the whole exercise: before, `TASKPRED` was never read, so every imported
   activity had zero float and read as critical, and the import reported success.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aec_api import schedule_cpm, schedule_engine, schedule_import

#: The keys every existing caller of `compute()` reads. Frozen deliberately.
LEGACY_KEYS = {
    "project_duration",
    "activity_count",
    "critical_count",
    "has_cycle",
    "activities",
    "critical_path",
}
LEGACY_ROW_KEYS = {
    "id",
    "ref",
    "name",
    "duration",
    "es",
    "ef",
    "ls",
    "lf",
    "total_float",
    "free_float",
    "critical",
    "predecessors",
}


def record(rid: str, ref: str, name: str, **data: object) -> dict:
    return {"id": rid, "ref": ref, "title": name, "data": {"name": name, **data}}


def chain() -> list[dict]:
    return [
        record("r1", "A1010", "Excavate", duration=5, start="2026-06-01"),
        record("r2", "A1020", "Foundations", duration=10, predecessors="A1010"),
        record("r3", "A1030", "Steel", duration=8, predecessors="A1020"),
    ]


# -- the contract every caller depends on ----------------------------------


def test_compute_returns_the_shape_it_always_returned() -> None:
    result = schedule_cpm.compute(chain())
    assert set(result) >= LEGACY_KEYS
    assert set(result["activities"][0]) >= LEGACY_ROW_KEYS
    assert result["activity_count"] == 3


def test_offsets_are_still_working_days_from_day_zero() -> None:
    """The unit every existing caller assumes. Changing it silently would
    misprice every EVM curve in the product.
    """
    result = schedule_cpm.compute(chain())
    rows = {r["ref"]: r for r in result["activities"]}
    assert rows["A1010"]["es"] == 0
    assert rows["A1010"]["ef"] == 5
    assert rows["A1020"]["es"] == 5
    assert rows["A1030"]["ef"] == 23


def test_an_empty_schedule_returns_an_empty_result_rather_than_raising() -> None:
    assert schedule_cpm.compute([])["activity_count"] == 0


def test_schedule_risk_can_still_import_the_private_helpers() -> None:
    """`schedule_risk` imports `_duration` and `_preds` by name."""
    from aec_api.schedule_cpm import _duration, _preds

    assert _duration({"duration": 7}) == 7
    assert _preds("A1010, A1020FS+3") == ["A1010", "A1020"]


def test_a_same_day_activity_is_one_day_of_work_not_zero() -> None:
    """The unambiguous half of the derived-duration question.

    `max(0, (finish - start).days)` returned **zero** for an activity recorded as
    starting and finishing on the same day, which made it a milestone and
    silently dropped it off the critical path.
    """
    assert _duration_of(start="2026-06-01", finish="2026-06-01") == 1


def test_the_derived_duration_convention_is_deliberately_unchanged() -> None:
    """The ambiguous half, left alone on purpose.

    `(finish - start).days` treats the stored finish as an exclusive boundary,
    so 1 to 11 January is ten days. P6's own `target_end_date` is inclusive,
    which would make it eleven. Changing it would add a day to every date-derived
    duration in every existing project -- a data migration, not a bug fix, and
    not one to make by side effect.
    """
    assert _duration_of(start="2026-01-01", finish="2026-01-11") == 10
    assert _duration_of(start="2026-06-01", finish="2026-06-05") == 4


def _duration_of(**data: object) -> int:
    return schedule_cpm._duration(dict(data))


# -- what the old engine could not do --------------------------------------


def test_relationship_types_and_lags_are_now_honoured() -> None:
    """Before, every predecessor token was a bare Finish-to-Start tie."""
    records = [
        record("r1", "A", "Frame", duration=10, start="2026-06-01"),
        record("r2", "B", "Follow", duration=5, predecessors="ASS+2"),
    ]
    result = schedule_cpm.compute(records)
    rows = {r["ref"]: r for r in result["activities"]}
    # Start-to-Start with two days of lag: B starts two working days after A.
    assert rows["B"]["es"] == 2


def test_calendars_change_the_answer() -> None:
    records = [
        record("r1", "A", "Five day", duration=10, start="2026-06-01", calendar="5D"),
        record("r2", "B", "Seven day", duration=10, start="2026-06-01", calendar="7D"),
    ]
    result = schedule_cpm.compute(records)
    rows = {r["ref"]: r for r in result["activities"]}
    assert rows["A"]["finish_date"] == "2026-06-12"
    assert rows["B"]["finish_date"] == "2026-06-10"


def test_computed_dates_are_returned_so_they_can_be_written_back() -> None:
    """The old engine only analysed; nothing ever wrote its answer anywhere."""
    result = schedule_cpm.compute(chain())
    row = result["activities"][0]
    assert row["start_date"] == "2026-06-01"
    assert row["finish_date"] == "2026-06-05"
    assert result["project_finish_date"]


def test_a_circular_network_returns_activities_but_no_fabricated_dates() -> None:
    """The old code broke the loop in dictionary order and returned a full set of
    dates anyway, flagged only by `has_cycle` -- and EVM consumed them.

    Callers that count or list activities still work; every computed field is
    `None` rather than a number that means nothing.
    """
    records = [
        record("r1", "A", "A", duration=1, predecessors="C"),
        record("r2", "B", "B", duration=1, predecessors="A"),
        record("r3", "C", "C", duration=1, predecessors="B"),
    ]
    result = schedule_cpm.compute(records)
    assert result["has_cycle"] is True
    assert result["activity_count"] == 3
    assert set(result["cycle"]) == {"r1", "r2", "r3"}
    for row in result["activities"]:
        assert row["es"] is None and row["total_float"] is None
        assert row["critical"] is False
    assert "CPM.CIRCULAR_LOGIC" in {i["code"] for i in result["issues"]}


def test_an_unresolvable_predecessor_is_reported_not_silently_dropped() -> None:
    """A typo in a predecessor field used to produce a missing dependency and
    no indication anywhere.
    """
    records = [
        record("r1", "A", "A", duration=5, start="2026-06-01"),
        record("r2", "B", "B", duration=5, predecessors="TYPO"),
    ]
    result = schedule_cpm.compute(records)
    codes = {i["code"] for i in result["issues"]}
    assert "MASSING.PREDECESSOR_UNRESOLVED" in codes


def test_predecessor_tokens_parse_type_and_lag() -> None:
    parsed = schedule_engine.parse_predecessor_tokens("A1010, A1020SS, A1030FF-2, A1040FS+3d")
    assert [(r, t.value, lag) for r, t, lag in parsed] == [
        ("A1010", "FS", 0),
        ("A1020", "SS", 0),
        ("A1030", "FF", -2),
        ("A1040", "FS", 3),
    ]


# -- the acceptance criterion ----------------------------------------------


XER = """ERMHDR\t19.12\t2026-08-08\tProject\tadmin
%T\tPROJECT
%F\tproj_id\tproj_short_name\tplan_start_date\tlast_recalc_date\tclndr_id
%R\t1\tTOWER\t2026-06-01 00:00\t2026-06-01 00:00\tC1
%T\tCALENDAR
%F\tclndr_id\tclndr_name\tday_hr_cnt\tdefault_flag\tclndr_data
%R\tC1\tStandard\t8\tY\t(0||DaysOfWeek()((1())(2()(0||1(s|08:00|f|16:00)))(3()(0||1(s|08:00|f|16:00)))(4()(0||1(s|08:00|f|16:00)))(5()(0||1(s|08:00|f|16:00)))(6()(0||1(s|08:00|f|16:00)))(7())))
%T\tTASK
%F\ttask_id\tproj_id\ttask_code\ttask_name\ttask_type\tclndr_id\ttarget_drtn_hr_cnt
%R\tT1\t1\tA1010\tExcavate\tTT_Task\tC1\t40
%R\tT2\t1\tA1020\tFoundations\tTT_Task\tC1\t80
%R\tT3\t1\tA1030\tSteel erection\tTT_Task\tC1\t64
%R\tT4\t1\tA1040\tSite hoarding\tTT_Task\tC1\t16
%T\tTASKPRED
%F\ttask_pred_id\ttask_id\tpred_task_id\tpred_type\tlag_hr_cnt
%R\t1\tT2\tT1\tPR_FS\t0
%R\t2\tT3\tT2\tPR_FS\t0
%R\t3\tT3\tT4\tPR_FS\t0
%E
"""


def test_a_real_xer_imports_with_its_logic_intact() -> None:
    """The acceptance criterion for the whole exercise.

    Before: `TASKPRED` was never read, so the network was flat and **every**
    activity reported as critical with zero float -- while the import reported
    success.
    """
    records, report = schedule_import.parse_full(XER)

    assert report["format"] == "xer"
    assert report["has_logic"] is True
    assert report["relationships"] == 3
    assert report["activities"] == 4

    predecessors = {r["activity_id"]: r["data"]["predecessors"] for r in records}
    assert predecessors["A1010"] == ""
    assert predecessors["A1020"] == "A1010"
    assert set(predecessors["A1030"].split(", ")) == {"A1020", "A1040"}


def test_the_imported_network_is_not_flat() -> None:
    """The measurable form of the same claim: fewer than 100% critical."""
    records, _report = schedule_import.parse_full(XER)
    as_records = [
        {"id": f"r{i}", "ref": r["activity_id"], "title": r["data"]["name"], "data": r["data"]}
        for i, r in enumerate(records)
    ]
    result = schedule_cpm.compute(as_records)

    assert result["has_cycle"] is False
    assert result["activity_count"] == 4
    # A1040 (site hoarding, 2 days) feeds the same successor as a 10-day chain,
    # so it carries float. A flat network would report all four as critical.
    assert result["critical_count"] < result["activity_count"]
    floats = {r["ref"]: r["total_float"] for r in result["activities"]}
    assert floats["A1040"] > 0
    assert floats["A1010"] == 0


def test_the_import_carries_calendars_and_durations_too() -> None:
    records, _ = schedule_import.parse_full(XER)
    first = next(r for r in records if r["activity_id"] == "A1010")
    assert first["data"]["duration"] == 5  # 40 hours at 8 hours per day
    assert first["data"]["calendar"] == "5D"


def test_an_xer_with_no_taskpred_says_so_rather_than_reporting_success() -> None:
    """The failure mode being closed: an import that *looks* like it worked."""
    stripped = XER.split("%T\tTASKPRED")[0] + "%E\n"
    _records, report = schedule_import.parse_full(stripped)
    assert report["has_logic"] is False
    codes = {i["code"] for i in report["issues"]}
    assert "XER.TASKPRED.MISSING" in codes


def test_format_detection() -> None:
    assert schedule_import.detect_format(XER) == "xer"
    assert (
        schedule_import.detect_format(
            '<?xml version="1.0"?><Project xmlns="http://schemas.microsoft.com/project"><Tasks/></Project>'
        )
        == "mspdi"
    )
    assert (
        schedule_import.detect_format(
            "<APIBusinessObjects><Project><Activity/></Project></APIBusinessObjects>"
        )
        == "pmxml"
    )
    assert schedule_import.detect_format("hello") == "unknown"


def test_pmxml_falls_back_and_says_it_carries_no_logic() -> None:
    records, report = schedule_import.parse_full(
        "<APIBusinessObjects><Project><Activity/></Project></APIBusinessObjects>"
    )
    assert records == []
    assert report["fell_back"] is True
    assert "IMPORT.PMXML_NO_LOGIC" in {i["code"] for i in report["issues"]}


def test_an_unreadable_upload_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="unrecognised schedule format"):
        schedule_import.parse_full("this is not a schedule")
