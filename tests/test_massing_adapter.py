"""The massing adoption kit's adapter, exercised here rather than only there.

`massingplan/integrations/massing/test_adoption.py` imports `aec_api` and can
only run inside the consumer. That left the adapter with **no test that runs in
this repo at all** — and both bugs Massing Core found on adoption were adapter
bugs, not engine bugs. A kit nobody exercises upstream is a kit whose defects
are found by the person adopting it.

These tests import the adapter directly, which works standalone: it depends on
`massingplan.core` and the standard library and nothing of massing's.
"""

from __future__ import annotations

import pytest

from massingplan.core.network import RelationType
from massingplan.integrations.massing import schedule_cpm, schedule_engine


def _record(rid: str, ref: str, duration: int, predecessors: str = "", **data: object) -> dict:
    """A record shaped the way `mod_schedule_activity` rows actually are."""
    return {
        "id": rid,
        "ref": ref,
        "data": {"name": ref, "duration": duration, "predecessors": predecessors, **data},
    }


def _chain(durations: list[int], prefix: str = "SA-") -> list[dict]:
    """A sequential FS chain, refs in the `PREFIX-NNNN` format massing generates."""
    records, previous = [], ""
    for index, duration in enumerate(durations):
        ref = f"{prefix}{index + 1:04d}"
        records.append(_record(f"id{index}", ref, duration, previous))
        previous = ref
    return records


# -- the token grammar -----------------------------------------------------


def test_a_hyphenated_ref_is_a_ref_and_not_a_negative_lag() -> None:
    """The bug that made a sequential chain come back fully parallel.

    `SA-0001` reads as either the activity `SA-0001` or the activity `SA` with a
    lag of minus one, and `PREFIX-NNNN` is exactly what the records module
    generates. The old single non-greedy pattern always preferred the shortest
    ref, so every token resolved to nothing, every relationship was dropped, and
    `project_duration` came back as the longest single activity.
    """
    known = {"SA-0001", "SA-0002"}
    assert schedule_engine.parse_predecessor_tokens("SA-0001", known) == [
        ("SA-0001", RelationType.FS, 0)
    ]


def test_a_real_lag_still_parses_when_the_stem_is_the_activity() -> None:
    """The other reading is still available -- it is just second in line."""
    assert schedule_engine.parse_predecessor_tokens("EST-12", {"EST"}) == [
        ("EST", RelationType.FS, -12)
    ]


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("A1010", ("A1010", RelationType.FS, 0)),
        ("A1010FS+3", ("A1010", RelationType.FS, 3)),
        ("A1010SS", ("A1010", RelationType.SS, 0)),
        ("A1010FF-2d", ("A1010", RelationType.FF, -2)),
        ("A1010SF+10", ("A1010", RelationType.SF, 10)),
        ("A1010 + 5", ("A1010", RelationType.FS, 5)),
    ],
)
def test_the_notation_planners_type_still_works(token: str, expected: tuple) -> None:
    assert schedule_engine.parse_predecessor_tokens(token, {"A1010"}) == [expected]


def test_a_ref_that_ends_in_a_relationship_type_wins_over_the_suffix_reading() -> None:
    """An activity genuinely called `PILE-FS` is not `PILE` with a Finish-Start.
    Resolution order settles it: a token naming a real activity is that
    activity.
    """
    assert schedule_engine.parse_predecessor_tokens("PILE-FS", {"PILE-FS", "PILE"}) == [
        ("PILE-FS", RelationType.FS, 0)
    ]


def test_several_tokens_in_one_field() -> None:
    known = {"SA-0001", "SA-0002"}
    assert schedule_engine.parse_predecessor_tokens("SA-0001, SA-0002SS+2", known) == [
        ("SA-0001", RelationType.FS, 0),
        ("SA-0002", RelationType.SS, 2),
    ]


def test_an_unresolvable_token_is_quoted_back_exactly_as_typed() -> None:
    """Reporting a stripped form would name something the planner never wrote.
    Telling someone who typed `SA-0001` that "SA matches no activity" sends them
    looking for the wrong thing.
    """
    assert schedule_engine.parse_predecessor_tokens("SA-0001", {"something-else"}) == [
        ("SA-0001", RelationType.FS, 0)
    ]
    assert schedule_engine.parse_predecessor_tokens("A1010FS+3", {"something-else"}) == [
        ("A1010FS+3", RelationType.FS, 0)
    ]


# -- the property the consumer's test asserts ------------------------------


def test_a_sequential_chain_has_a_duration_equal_to_the_sum() -> None:
    """massing's `test_productivity` asserts exactly this, and it was right.

    `POST /schedule/from-estimate` writes one activity per trade chained FS, so
    the project duration is the sum of the durations. It came back as the
    longest single activity, which is what a network with no logic looks like.
    """
    durations = [12, 34, 9, 5]
    result = schedule_cpm.compute(_chain(durations))
    assert result["project_duration"] == sum(durations)
    assert result["critical_count"] == len(durations), "a pure chain is entirely critical"
    assert result["issues"] == []


def test_the_chain_survives_every_ref_format_massing_generates() -> None:
    for prefix in ("SA-", "EST-", "ACT", "WBS-1-", "A"):
        durations = [3, 4, 5]
        result = schedule_cpm.compute(_chain(durations, prefix))
        assert result["project_duration"] == sum(durations), prefix
        assert result["issues"] == [], prefix


def test_a_genuinely_missing_predecessor_is_still_reported() -> None:
    """The fix must not turn an unresolvable token into a silent pass. Reporting
    it is the improvement over the old engine, which dropped it without a word.
    """
    records = [_record("id0", "SA-0001", 5, "NOT-A-REF")]
    result = schedule_cpm.compute(records)
    assert [i["code"] for i in result["issues"]] == ["MASSING.PREDECESSOR_UNRESOLVED"]


# -- the legacy dict contract ----------------------------------------------


def test_float_is_numeric_even_for_completed_work() -> None:
    """The engine reports `None` for a completed activity, which is the better
    answer -- finished work has no float, and 0 would put it on the critical
    path. But `px.optimize` filters on `0 < total_float <= 5` behind an
    `_open()` test that decides completeness from `percent`, while the engine
    decides it from actual dates. An activity with an actual finish and no
    recorded percent is open to one and complete to the other, and the
    comparison raises TypeError.
    """
    records = [
        _record("id0", "SA-0001", 5, "", actual_start="2026-06-01", actual_finish="2026-06-05"),
        _record("id1", "SA-0002", 5, "SA-0001"),
    ]
    rows = schedule_cpm.compute(records)["activities"]
    for row in rows:
        assert isinstance(row["total_float"], int), row["ref"]
        assert isinstance(row["free_float"], int), row["ref"]
        # What every existing consumer does, and it must not raise.
        assert isinstance(0 < row["total_float"] <= 5, bool)


def test_the_honest_value_is_still_available_alongside() -> None:
    """Numeric for the old key, `None` for the new one. Losing the distinction
    between "no float" and "zero float" to keep the contract would be paying
    twice.
    """
    records = [
        _record("id0", "SA-0001", 5, "", actual_start="2026-06-01", actual_finish="2026-06-05"),
        _record("id1", "SA-0002", 5, "SA-0001"),
    ]
    rows = {r["ref"]: r for r in schedule_cpm.compute(records)["activities"]}
    done, todo = rows["SA-0001"], rows["SA-0002"]
    assert done["total_float"] == 0 and done["total_float_days"] is None
    assert done["has_float"] is False
    assert todo["total_float"] == 0 and todo["total_float_days"] == 0
    assert todo["has_float"] is True


def test_negative_float_is_not_clamped_in_either_key() -> None:
    """A late constraint produces negative float, and that number is the output
    the planner is looking for. `_numeric_float` only replaces `None`.
    """
    records = [
        _record("id0", "SA-0001", 20, ""),
        _record(
            "id1",
            "SA-0002",
            20,
            "SA-0001",
            constraint="finish on or before",
            constraint_date="2026-06-15",
        ),
    ]
    rows = {r["ref"]: r for r in schedule_cpm.compute(records)["activities"]}
    assert rows["SA-0002"]["total_float"] < 0
    assert rows["SA-0002"]["total_float_days"] == rows["SA-0002"]["total_float"]


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


def test_the_legacy_shape_is_intact() -> None:
    """New keys are fine; a missing or renamed one is a production break."""
    result = schedule_cpm.compute(_chain([3, 4]))
    assert set(result) >= LEGACY_KEYS
    for row in result["activities"]:
        assert set(row) >= LEGACY_ROW_KEYS


def test_an_empty_schedule_does_not_raise() -> None:
    result = schedule_cpm.compute([])
    assert result["project_duration"] == 0
    assert result["activities"] == []
    assert result["has_cycle"] is False


def test_a_cycle_refuses_rather_than_inventing_dates() -> None:
    """Returning dates computed by breaking a loop in dictionary order is a
    confident wrong answer that EVM then consumes as fact.
    """
    records = [
        _record("id0", "SA-0001", 5, "SA-0002"),
        _record("id1", "SA-0002", 5, "SA-0001"),
    ]
    result = schedule_cpm.compute(records)
    assert result["has_cycle"] is True
    assert result["project_duration"] == 0
    assert set(result["cycle"]) == {"id0", "id1"}
    assert all(r["start_date"] is None for r in result["activities"])
