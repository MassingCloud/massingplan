"""Adapter between `mod_schedule_activity` records and the massingplan engine.

The module engine stores every activity as a row with a JSON `data` blob, and
the vendored engine (`services/api/src/massingplan`, see its VENDOR.md) works on
typed `Task`/`Link`/`WorkCalendar` values. This is the only place the two meet.
Nothing else in the API should import `massingplan` directly -- keeping the
translation in one file is what makes an upstream re-sync a copy rather than a
refactor.

What the previous engine (`schedule_cpm.py`, 105 lines) could not represent, and
this adapter now carries through:

* **SS, FF and SF relationships, with leads and lags.** Predecessor tokens are
  parsed as `<ref>[FS|SS|FF|SF][+/-N]`, the notation planners already type.
  Before, every token was a bare Finish-to-Start tie.
* **Calendars.** An activity can name one; without them the forward pass could
  not tell a five-day week from a seven-day one.
* **Constraints, actual dates and remaining duration**, so a progressed schedule
  reschedules from the data date instead of from day zero.

The one idea worth keeping from the old module is its predecessor alias map:
a token may be an activity `ref`, its `wbs` code, or a raw record id, because
the engine's own output emits ids for activities that have no ref. That
resolution lives here rather than in the engine, which knows nothing about
records.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from massingplan.core.constraints import ConstraintType
from massingplan.core.constraints import parse as parse_constraint
from massingplan.core.issues import IssueLog
from massingplan.core.network import ActivityKind, Link, RelationType, Task
from massingplan.core.timeaxis import WorkCalendar, WorkPattern

#: `<ref><type><signed lag>` -- "A1010", "A1010SS", "A1010FS+3", "A1010FF-2d".
_TOKEN = re.compile(
    r"^(?P<ref>.+?)(?:(?P<type>FS|SS|FF|SF))?(?:(?P<lag>[+-]\s*\d+)\s*d?)?$",
    re.IGNORECASE,
)

_KINDS = {
    "task": ActivityKind.TASK,
    "milestone": ActivityKind.FINISH_MILESTONE,
    "start milestone": ActivityKind.START_MILESTONE,
    "finish milestone": ActivityKind.FINISH_MILESTONE,
    "summary": ActivityKind.WBS_SUMMARY,
    "level of effort": ActivityKind.LEVEL_OF_EFFORT,
}

#: Named work patterns an activity may reference by `data.calendar`. A project
#: that says nothing gets the five-day week, which is what the old engine
#: assumed implicitly and never wrote down.
CALENDARS: dict[str, WorkCalendar] = {
    "5D": WorkCalendar("5D", "Mon-Fri", WorkPattern(frozenset({0, 1, 2, 3, 4}))),
    "6D": WorkCalendar("6D", "Mon-Sat", WorkPattern(frozenset({0, 1, 2, 3, 4, 5}))),
    "7D": WorkCalendar("7D", "Every day", WorkPattern(frozenset({0, 1, 2, 3, 4, 5, 6}))),
}
DEFAULT_CALENDAR = "5D"


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _duration_days(data: dict) -> int:
    """Duration in days: the stated field if usable, else derived from the dates.

    **The derived convention is deliberately unchanged**: `(finish - start).days`,
    so a span of 1 to 11 January is ten days. That treats the stored `finish` as
    an exclusive boundary. P6's own `target_end_date` is inclusive, which would
    make it eleven -- so this is arguably off by one against the source files.
    It is left alone on purpose: every existing record in every existing project
    was entered under this reading, and silently adding a day to all of them is
    a data migration disguised as a bug fix. Worth deciding explicitly; not worth
    deciding by side effect.

    What *is* fixed is the unambiguous case. The previous engine returned
    `max(0, delta)`, so an activity recorded as starting and finishing on the
    same day came back with **zero** duration -- which made it a milestone and
    silently removed it from the critical path. A day of work is a day.
    """
    explicit = _as_int(data.get("duration"))
    if explicit is not None and explicit >= 0:
        return explicit
    start, finish = _as_date(data.get("start")), _as_date(data.get("finish"))
    if start and finish and finish >= start:
        return max(1, (finish - start).days)
    return 1


def parse_predecessor_tokens(raw: Any) -> list[tuple[str, RelationType, int]]:
    """`"A1010FS+3, A1020SS"` -> `[("A1010", FS, 3), ("A1020", SS, 0)]`."""
    if not raw:
        return []
    out: list[tuple[str, RelationType, int]] = []
    for chunk in str(raw).replace(";", ",").split(","):
        token = chunk.strip()
        if not token:
            continue
        match = _TOKEN.match(token)
        if match is None:
            out.append((token, RelationType.FS, 0))
            continue
        ref = (match.group("ref") or "").strip()
        if not ref:
            continue
        kind = match.group("type")
        lag = match.group("lag")
        out.append(
            (
                ref,
                RelationType[kind.upper()] if kind else RelationType.FS,
                int(lag.replace(" ", "")) if lag else 0,
            )
        )
    return out


def build_network(
    records: list[dict], *, issues: IssueLog | None = None
) -> tuple[list[Task], list[Link], dict[str, WorkCalendar], IssueLog]:
    """Map activity records to the engine's inputs.

    Unresolvable predecessor tokens are reported rather than dropped in silence.
    The old engine discarded them, so a typo in a predecessor field produced a
    schedule with a missing dependency and no indication anywhere.
    """
    issues = issues if issues is not None else IssueLog()
    tasks: list[Task] = []
    alias: dict[str, str] = {}
    token_lists: dict[str, list[tuple[str, RelationType, int]]] = {}

    for record in records:
        data = record.get("data") or {}
        rid = record["id"]
        for key in (record.get("ref"), data.get("wbs")):
            if key:
                alias[str(key).strip()] = rid
    # A record id is a legitimate token: the engine emits ids for activities with
    # no ref, so refusing to consume one would make its output unusable as its
    # own input. `setdefault`, so an explicit ref or wbs always wins over an id
    # that happens to collide with one.
    for record in records:
        alias.setdefault(str(record["id"]), record["id"])

    for record in records:
        data = record.get("data") or {}
        rid = record["id"]
        kind = _KINDS.get(str(data.get("activity_type", "")).strip().lower(), ActivityKind.TASK)
        duration = 0 if kind.is_milestone else _duration_days(data)

        calendar_id = str(data.get("calendar") or DEFAULT_CALENDAR).upper()
        if calendar_id not in CALENDARS:
            issues.warn(
                "MASSING.CALENDAR_UNKNOWN",
                f"{record.get('ref') or rid}: calendar {calendar_id!r} is not defined",
                f"used {DEFAULT_CALENDAR}",
                row_key=rid,
                field_name="calendar",
                raw_value=calendar_id,
            )
            calendar_id = DEFAULT_CALENDAR

        constraint = parse_constraint(data.get("constraint"))
        constraint_date = _as_date(data.get("constraint_date"))
        if constraint.needs_date and constraint_date is None:
            constraint = ConstraintType.NONE

        percent = data.get("percent")
        percent_complete = None
        parsed_percent = None if percent in (None, "") else float(percent)
        if parsed_percent is not None:
            percent_complete = parsed_percent / 100 if parsed_percent > 1 else parsed_percent

        tasks.append(
            Task(
                id=rid,
                name=record.get("title") or data.get("name") or rid,
                duration_days=duration,
                calendar_id=calendar_id,
                kind=kind,
                constraint=constraint,
                constraint_date=constraint_date,
                actual_start=_as_date(data.get("actual_start")),
                actual_finish=_as_date(data.get("actual_finish")),
                remaining_days=_as_int(data.get("remaining_duration")),
                percent_complete=percent_complete,
            )
        )
        token_lists[rid] = parse_predecessor_tokens(data.get("predecessors"))

    known = {t.id for t in tasks}
    links: list[Link] = []
    seen: set[tuple[str, str]] = set()
    for successor, tokens in token_lists.items():
        for ref, rel, lag in tokens:
            predecessor = alias.get(ref)
            if predecessor is None:
                issues.warn(
                    "MASSING.PREDECESSOR_UNRESOLVED",
                    f"{successor}: predecessor {ref!r} matches no activity",
                    "relationship dropped",
                    row_key=successor,
                    field_name="predecessors",
                    raw_value=ref,
                )
                continue
            if predecessor == successor or predecessor not in known:
                continue
            if (predecessor, successor) in seen:
                continue
            seen.add((predecessor, successor))
            links.append(Link(predecessor, successor, rel, lag))

    return tasks, links, dict(CALENDARS), issues


def data_date_for(records: list[dict], fallback: date | None = None) -> date:
    """The date to schedule from: the latest recorded actual, else the earliest
    planned start, else today.

    Scheduling a progressed job from its planned start reports every completed
    activity as still to do.
    """
    actuals: list[date] = []
    starts: list[date] = []
    for record in records:
        data = record.get("data") or {}
        for key in ("actual_finish", "actual_start"):
            parsed = _as_date(data.get(key))
            if parsed:
                actuals.append(parsed)
        planned = _as_date(data.get("start"))
        if planned:
            starts.append(planned)
    if actuals:
        return max(actuals)
    if starts:
        return min(starts)
    return fallback or date.today()


def to_legacy_rows(outcome: Any, tasks: list[Task], links: list[Link], records: list[dict]) -> dict:
    """Render an engine result in the shape `schedule_cpm.compute()` always returned.

    Existing callers -- EVM, the extension-of-time analysis, resource loading,
    the vitals dashboard, the Gantt renderer -- read working-day offsets from day
    zero, so those are preserved exactly. Real dates, calendars, constraint
    violations and the driving path are added as *new* keys, which no caller can
    be broken by.
    """
    network = outcome.network
    by_ref = {r["id"]: r.get("ref") for r in records}
    project_start = network.project_start
    default_cal = CALENDARS[DEFAULT_CALENDAR]
    incoming: dict[str, list[str]] = {}
    for link in links:
        incoming.setdefault(link.successor, []).append(link.predecessor)

    def offset(instant: int, calendar_id: str) -> int:
        cal = CALENDARS.get(calendar_id, default_cal)
        return cal.count_working_days(project_start, instant)

    rows = []
    for task in tasks:
        aid = task.id
        dates = outcome.dates[aid]
        rows.append(
            {
                "id": aid,
                "ref": by_ref.get(aid),
                "name": task.name,
                "duration": task.duration_days,
                "es": offset(network.early_start[aid], task.calendar_id),
                "ef": offset(network.early_finish[aid], task.calendar_id),
                "ls": offset(network.late_start[aid], task.calendar_id),
                "lf": offset(network.late_finish[aid], task.calendar_id),
                "total_float": network.total_float_days[aid],
                "free_float": network.free_float_days[aid],
                "critical": network.is_critical(aid),
                "predecessors": incoming.get(aid, []),
                # -- new, additive --------------------------------------------
                "start_date": dates.start.isoformat(),
                "finish_date": dates.finish.isoformat(),
                "late_start_date": dates.late_start.isoformat(),
                "late_finish_date": dates.late_finish.isoformat(),
                "calendar": task.calendar_id,
                "status": dates.status.value,
                "remaining_days": dates.remaining_days,
                "on_driving_path": dates.is_longest_path,
                "constraint_satisfied": dates.constraint_satisfied,
            }
        )
    rows.sort(key=lambda r: (r["es"], r["ef"], r["id"]))

    return {
        "project_duration": offset(network.project_finish, DEFAULT_CALENDAR),
        "activity_count": len(rows),
        "critical_count": sum(1 for r in rows if r["critical"]),
        # The old engine returned `has_cycle: True` and carried on with wrong
        # numbers. The new one raises, and the shim converts that into this flag
        # plus an empty result -- so a cyclic network can no longer be mistaken
        # for a scheduled one.
        "has_cycle": False,
        "activities": rows,
        "critical_path": [r["ref"] or r["id"] for r in rows if r["critical"]],
        # -- new, additive ------------------------------------------------
        "data_date": outcome.data_date.isoformat(),
        "project_start_date": outcome.project_start.isoformat(),
        "project_finish_date": outcome.project_finish.isoformat(),
        "driving_path": list(outcome.longest_path),
        "violations": [v.to_dict() for v in outcome.violations],
        "issues": [i.to_dict() for i in outcome.issues],
    }
