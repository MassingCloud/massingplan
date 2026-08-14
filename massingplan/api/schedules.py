"""The scheduling operations, as plain functions over JSON-safe values.

Every function here takes primitives and returns a dict that survives
``json.dumps`` with no custom encoder. That is what makes the same surface
mountable under Flask here and under FastAPI in massing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from ..core import compare as compare_mod
from ..core import health, levelling, resources, risk, windows
from ..core.constraints import parse as parse_constraint
from ..core.model import Calendar, ExchangeSchedule
from ..core.network import (
    ActivityKind,
    LagCalendar,
    Link,
    ProgressMode,
    RelationType,
    SchedulerOptions,
    Task,
)
from ..core.schedule import ScheduleOutcome, schedule_network
from ..core.timeaxis import WorkCalendar, WorkPattern
from .errors import Unsupported, ValidationFailed


def _date(value: object, field: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValidationFailed(f"{field}: {value!r} is not an ISO date (YYYY-MM-DD)") from exc


def _calendars(payload: Sequence[Mapping[str, Any]] | None) -> dict[str, WorkCalendar]:
    if not payload:
        from ..core.timeaxis import standard_calendar

        return {"STD": standard_calendar()}
    out: dict[str, WorkCalendar] = {}
    for entry in payload:
        cal_id = str(entry.get("id") or "STD")
        weekdays = entry.get("working_weekdays") or [0, 1, 2, 3, 4]
        holidays = frozenset(
            d for d in (_date(h, "holiday") for h in entry.get("holidays") or []) if d
        )
        extra = frozenset(
            d for d in (_date(h, "extra_work_day") for h in entry.get("extra_work_days") or []) if d
        )
        out[cal_id] = WorkCalendar(
            id=cal_id,
            name=str(entry.get("name") or cal_id),
            pattern=WorkPattern(frozenset(int(d) for d in weekdays), holidays, extra),
            hours_per_day=float(entry.get("hours_per_day") or 8.0),
        )
    return out


def _tasks_and_links(
    payload: Sequence[Mapping[str, Any]],
    calendars: Mapping[str, WorkCalendar],
) -> tuple[list[Task], list[Link]]:
    if not payload:
        raise ValidationFailed("no activities supplied")
    default_calendar = next(iter(calendars))
    tasks: list[Task] = []
    links: list[Link] = []

    for entry in payload:
        aid = str(entry.get("id") or "").strip()
        if not aid:
            raise ValidationFailed("every activity needs an id", detail=entry)
        kind_raw = str(entry.get("kind") or "task")
        try:
            kind = ActivityKind(kind_raw)
        except ValueError as exc:
            raise ValidationFailed(
                f"{aid}: unknown activity kind {kind_raw!r}",
                detail=[k.value for k in ActivityKind],
            ) from exc
        try:
            tasks.append(
                Task(
                    id=aid,
                    name=str(entry.get("name") or aid),
                    duration_days=int(entry.get("duration_days") or 0),
                    calendar_id=str(entry.get("calendar_id") or default_calendar),
                    kind=kind,
                    constraint=parse_constraint(entry.get("constraint")),
                    constraint_date=_date(entry.get("constraint_date"), f"{aid}.constraint_date"),
                    actual_start=_date(entry.get("actual_start"), f"{aid}.actual_start"),
                    actual_finish=_date(entry.get("actual_finish"), f"{aid}.actual_finish"),
                    remaining_days=(
                        None
                        if entry.get("remaining_days") in (None, "")
                        else int(entry["remaining_days"])
                    ),
                    percent_complete=(
                        None
                        if entry.get("percent_complete") in (None, "")
                        else float(entry["percent_complete"])
                    ),
                )
            )
        except ValueError as exc:
            # The engine's own validation messages are already specific; wrap
            # rather than restate, so the caller sees the real reason.
            raise ValidationFailed(str(exc)) from exc

        for pred in entry.get("predecessors") or []:
            if isinstance(pred, str):
                links.append(Link(pred, aid))
                continue
            try:
                rel = RelationType(str(pred.get("type") or "FS").upper())
            except ValueError as exc:
                raise ValidationFailed(
                    f"{aid}: unknown relationship type {pred.get('type')!r}",
                    detail=[r.value for r in RelationType],
                ) from exc
            links.append(Link(str(pred.get("id")), aid, rel, int(pred.get("lag_days") or 0)))
    return tasks, links


def _options(payload: Mapping[str, Any] | None) -> SchedulerOptions:
    payload = payload or {}
    try:
        return SchedulerOptions(
            progress_mode=ProgressMode(str(payload.get("progress_mode") or "retained_logic")),
            lag_calendar=LagCalendar(str(payload.get("lag_calendar") or "predecessor")),
            must_finish_by=_date(payload.get("must_finish_by"), "must_finish_by"),
            open_ends_are_critical=bool(payload.get("open_ends_are_critical")),
        )
    except ValueError as exc:
        raise ValidationFailed(f"unusable scheduler options: {exc}") from exc


def chart_rows(
    outcome: Any,
    links: Sequence[Any],
    labels: Mapping[str, tuple[str, str]] | None = None,
    kinds: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """`to_rows()` plus the two things a chart needs and persistence does not.

    **`predecessors`.** The Gantt draws dependency arrows from this key, and it
    was never in the payload -- `to_rows()` does not carry relationships,
    because the persistence contract stores them in their own table. The
    renderer read `(row.predecessors || [])`, got an empty array on every page,
    and drew nothing. No error, no blank space, just a chart quietly missing the
    feature its own docstring leads with. The `|| []` is what made it silent.

    Each entry is `{"id", "type", "lag_days"}` -- **the same shape this module
    accepts as input**. The first version emitted bare id strings, which cost
    twice. The renderer could not tell a Start-Start tie from a Finish-Start
    one, so it drew every arrow predecessor-finish to successor-start and every
    SS and FF link pointed backwards in time. And `_tasks_and_links` reads a
    bare string as Finish-Start with zero lag, so a client that fed a response
    back in silently flattened every relationship type and every lag. Matching
    the input shape fixes both, and makes the API round-trip exactly.

    **`code` and `name`.** Rows carry the internal id, which is what persistence
    needs and what nobody recognises. On a stored project that id is 32 hex
    characters, so every bar was labelled with a UUID.

    All three are added here rather than inside `to_rows()`, whose key set is
    frozen by test on purpose.

    **`kind`.** A milestone is a point in time, but a *finish* milestone dated
    the same day its predecessor ends belongs at the end of that day, not its
    start -- the predecessor's bar occupies the whole day. Without the kind the
    renderer cannot tell the two apart and draws the arrow into it backwards by
    one day-width. `to_rows()` does not carry the kind because persistence keeps
    it on the activity row, so it is supplied here.

    `links` is required, deliberately. It defaulted to `()` for one commit, and
    a default that yields "no relationships" is the arrows bug waiting to be
    reintroduced by a caller who simply forgets an argument. Forgetting it is
    now a TypeError at the call site.
    """
    incoming: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        incoming.setdefault(str(link.successor), []).append(
            {
                "id": str(link.predecessor),
                "type": link.type.value,
                "lag_days": link.lag_days,
            }
        )

    known = dict(labels or {})
    known_kinds = dict(kinds or {})
    rows = []
    for row in outcome.to_rows():
        activity_id = str(row["activity_id"])
        code, name = known.get(activity_id, ("", ""))
        rows.append(
            {
                **row,
                "code": code or activity_id,
                "name": name,
                "kind": known_kinds.get(activity_id, "task"),
                "predecessors": incoming.get(activity_id, []),
            }
        )
    return rows


def schedule_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Schedule a network described as JSON. The primary endpoint."""
    calendars = _calendars(payload.get("calendars"))
    tasks, links = _tasks_and_links(payload.get("activities") or [], calendars)
    data_date = _date(payload.get("data_date"), "data_date")

    from ..core.graph import ScheduleCycleError

    try:
        outcome = schedule_network(
            tasks, links, calendars, data_date=data_date, options=_options(payload.get("options"))
        )
    except ScheduleCycleError as exc:
        raise ValidationFailed(
            "circular logic: " + " -> ".join(exc.cycle), detail={"cycle": exc.cycle}
        ) from exc
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc

    return {
        **outcome.summary(),
        "activities": chart_rows(outcome, links, kinds={t.id: t.kind.value for t in tasks}),
        "violations": [v.to_dict() for v in outcome.violations],  # type: ignore[attr-defined]
        "issues": [i.to_dict() for i in outcome.issues],
    }


def analyse(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Schedule, then run the DCMA assessment over the result."""
    calendars = _calendars(payload.get("calendars"))
    tasks, links = _tasks_and_links(payload.get("activities") or [], calendars)
    outcome = _schedule(tasks, links, calendars, payload)

    baseline = {
        str(k): d
        for k, v in (payload.get("baseline_finish") or {}).items()
        if (d := _date(v, "baseline_finish")) is not None
    }
    report = health.assess(
        outcome,
        tasks,
        links,
        calendars,
        baseline_finish=baseline or None,
        resourced_activity_ids=payload.get("resourced_activity_ids"),
    )
    return {"schedule": outcome.summary(), "health": report.to_dict()}


def simulate_risk(payload: Mapping[str, Any]) -> dict[str, Any]:
    calendars = _calendars(payload.get("calendars"))
    tasks, links = _tasks_and_links(payload.get("activities") or [], calendars)
    iterations = int(payload.get("iterations") or 2000)
    if not 1 <= iterations <= 20_000:
        raise ValidationFailed(
            f"iterations must be between 1 and 20000, got {iterations}",
            detail="a run larger than that is a batch job, not a request",
        )
    try:
        distribution = risk.Distribution(str(payload.get("distribution") or "pert"))
    except ValueError as exc:
        raise ValidationFailed(f"unknown distribution {payload.get('distribution')!r}") from exc

    result = risk.simulate(
        tasks,
        links,
        calendars,
        iterations=iterations,
        distribution=distribution,
        seed=payload.get("seed", 12345),
        data_date=_date(payload.get("data_date"), "data_date"),
        options=_options(payload.get("options")),
    )
    return result.to_dict()


def schedule_linear(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Location-based (line-of-balance) scheduling.

    Returns the flow *and* the network it emits, because the network is the
    point: location-based scheduling here is a way of generating activities and
    logic, not a second scheduler with its own answer. The caller can hand the
    `activities` straight to `/schedule` and get identical dates.
    """
    from ..core.locations import (
        LinearScheduleError,
        LinearTask,
        Location,
        compute,
        to_network,
    )

    raw_locations = payload.get("locations") or []
    if not raw_locations:
        raise ValidationFailed("a linear schedule needs a location breakdown")
    raw_tasks = payload.get("tasks") or []
    if not raw_tasks:
        raise ValidationFailed("a linear schedule needs at least one task")

    try:
        locations = [
            Location(
                id=str(entry.get("id") or ""),
                name=str(entry.get("name") or ""),
                sequence=int(entry.get("sequence") or index),
            )
            for index, entry in enumerate(raw_locations)
        ]
        tasks = [
            LinearTask(
                id=str(entry.get("id") or ""),
                name=str(entry.get("name") or ""),
                duration_days=int(entry.get("duration_days") or 1),
                quantities={str(k): float(v) for k, v in (entry.get("quantities") or {}).items()},
                rate=None if entry.get("rate") in (None, "") else float(entry["rate"]),
                buffer_days=int(entry.get("buffer_days") or 0),
                crews=int(entry.get("crews") or 1),
                calendar_id=str(entry.get("calendar_id") or "STD"),
            )
            for entry in raw_tasks
        ]
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"unusable linear schedule: {exc}") from exc

    calendars = _calendars(payload.get("calendars"))
    calendar = next(iter(calendars.values()))
    start = _date(payload.get("start"), "start") or _date(payload.get("data_date"), "data_date")
    if start is None:
        raise ValidationFailed("a linear schedule needs a `start` date to flow from")

    try:
        result = compute(tasks, locations)
        net_tasks, links, _cals = to_network(
            result, tasks, locations, start=start, calendar=calendar
        )
    except LinearScheduleError as exc:
        raise ValidationFailed(str(exc)) from exc

    return {
        "start": start.isoformat(),
        "duration_working_days": result.duration_days,
        "segments": result.to_rows(start=start, calendar=calendar),
        "interferences": [i.to_dict() for i in result.interferences],
        # Named rather than buried: this is what continuity cost, and it is the
        # number that says whether location-based scheduling paid for itself.
        "continuity_cost_days": result.continuity_cost_days,
        "issues": result.issues.to_list(),
        "activities": [
            {
                "id": task.id,
                "name": task.name,
                "duration_days": task.duration_days,
                "calendar_id": task.calendar_id,
                # The constraint is what holds each crew's line where the flow
                # put it. Omitting it here served activities whose logic was
                # right and whose *placement* silently reverted to earliest
                # start -- which fragments every crew, the one thing this
                # module exists to prevent. The round-trip test caught it.
                "constraint": task.constraint.value,
                "constraint_date": (
                    task.constraint_date.isoformat() if task.constraint_date else None
                ),
                "predecessors": [
                    {"id": link.predecessor, "type": link.type.value, "lag_days": link.lag_days}
                    for link in links
                    if link.successor == task.id
                ],
            }
            for task in net_tasks
        ],
    }


def schedule_takt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Takt planning: a fixed rhythm through the zones.

    Same contract as `schedule_linear` and deliberately so -- both return the
    flow *and* the network it emits, because neither is a second scheduler.
    Hand the `activities` to `/schedule` and the dates come back identical.

    `takt_days` is optional. Omitted, it uses the shortest feasible rhythm and
    reports it, rather than defaulting to a week and quietly overloading the
    bottleneck trade -- a plan that cannot be built but looks like one is worse
    than a refusal.
    """
    from ..core.takt import TaktError, Wagon, minimum_takt
    from ..core.takt import plan as build_takt
    from ..core.takt import to_network as takt_network

    raw_zones = payload.get("zones") or payload.get("locations") or []
    if not raw_zones:
        raise ValidationFailed("a takt plan needs a zone breakdown")
    raw_wagons = payload.get("wagons") or []
    if not raw_wagons:
        raise ValidationFailed("a takt plan needs at least one wagon")

    from ..core.locations import Location

    try:
        zones = [
            Location(
                id=str(entry.get("id") or ""),
                name=str(entry.get("name") or ""),
                sequence=int(entry.get("sequence") or index),
            )
            for index, entry in enumerate(raw_zones)
        ]
        wagons = [
            Wagon(
                id=str(entry.get("id") or ""),
                name=str(entry.get("name") or ""),
                work_content={
                    str(k): float(v) for k, v in (entry.get("work_content") or {}).items()
                },
                default_work=float(entry.get("default_work") or 0.0),
                max_crews=int(entry.get("max_crews") or 4),
            )
            for entry in raw_wagons
        ]
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"unusable takt plan: {exc}") from exc

    calendars = _calendars(payload.get("calendars"))
    calendar = next(iter(calendars.values()))
    start = _date(payload.get("start"), "start") or _date(payload.get("data_date"), "data_date")
    if start is None:
        raise ValidationFailed("a takt plan needs a `start` date to run from")

    try:
        feasible, bottleneck = minimum_takt(wagons, zones)
        raw_takt = payload.get("takt_days")
        takt_days = feasible if raw_takt in (None, "") else int(raw_takt)
        result = build_takt(wagons, zones, takt_days=takt_days)
        net_tasks, links, _cals = takt_network(
            result, wagons, zones, start=start, calendar=calendar
        )
    except TaktError as exc:
        raise ValidationFailed(str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"unusable takt plan: {exc}") from exc

    return {
        "start": start.isoformat(),
        "takt_days": result.takt_days,
        "minimum_takt_days": feasible,
        "bottleneck": bottleneck,
        "duration_working_days": result.duration_days,
        "slots": result.to_rows(start=start, calendar=calendar),
        "crews": result.crews,
        # Unrounded, and named. This is what the rhythm cost, and it is the
        # takt equivalent of `continuity_cost_days` above -- the number the
        # method has to be justified against rather than the one it advertises.
        "utilisation": {k: round(v, 4) for k, v in result.utilisation.items()},
        "idle_crew_days": round(result.idle_crew_days, 2),
        "overloaded": list(result.overloaded),
        "issues": result.issues.to_list(),
        "activities": [
            {
                "id": task.id,
                "name": task.name,
                "duration_days": task.duration_days,
                "calendar_id": task.calendar_id,
                "constraint": task.constraint.value,
                "constraint_date": (
                    task.constraint_date.isoformat() if task.constraint_date else None
                ),
                "predecessors": [
                    {"id": link.predecessor, "type": link.type.value, "lag_days": link.lag_days}
                    for link in links
                    if link.successor == task.id
                ],
            }
            for task in net_tasks
        ],
    }


def level_resources(payload: Mapping[str, Any]) -> dict[str, Any]:
    calendars = _calendars(payload.get("calendars"))
    tasks, links = _tasks_and_links(payload.get("activities") or [], calendars)
    outcome = _schedule(tasks, links, calendars, payload)

    demands = [
        resources.Demand(
            str(d["activity_id"]), str(d["resource_id"]), float(d.get("units_per_day") or 1.0)
        )
        for d in payload.get("demands") or []
    ]
    availability = [
        resources.ResourceAvailability(
            str(a["resource_id"]),
            float(a.get("units_per_day") or 1.0),
            calendar_id=a.get("calendar_id"),
        )
        for a in payload.get("availability") or []
    ]
    try:
        horizon = levelling.LevellingHorizon(str(payload.get("horizon") or "within_float"))
        mode = levelling.LevellingMode(str(payload.get("mode") or "advisory"))
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc

    result = levelling.level(
        levelling.LevellingRequest(
            outcome=outcome,
            tasks=tasks,
            links=links,
            calendars=calendars,
            demands=demands,
            availability=availability,
            horizon=horizon,
            mode=mode,
        )
    )
    return result.to_dict()


def compare_baselines(payload: Mapping[str, Any]) -> dict[str, Any]:
    baseline_payload = payload.get("baseline") or {}
    current_payload = payload.get("current") or {}
    if not baseline_payload or not current_payload:
        raise ValidationFailed("both `baseline` and `current` are required")

    base_cals = _calendars(baseline_payload.get("calendars") or payload.get("calendars"))
    curr_cals = _calendars(current_payload.get("calendars") or payload.get("calendars"))
    base_tasks, base_links = _tasks_and_links(baseline_payload.get("activities") or [], base_cals)
    curr_tasks, curr_links = _tasks_and_links(current_payload.get("activities") or [], curr_cals)

    base_out = _schedule(base_tasks, base_links, base_cals, baseline_payload)
    curr_out = _schedule(curr_tasks, curr_links, curr_cals, current_payload)

    try:
        match = compare_mod.MatchKey(str(payload.get("match") or "id"))
    except ValueError as exc:
        raise ValidationFailed(f"unknown match key {payload.get('match')!r}") from exc

    result = compare_mod.compare(
        base_out,
        curr_out,
        baseline_network=(base_tasks, base_links),
        current_network=(curr_tasks, curr_links),
        match=match,
        baseline_codes=payload.get("baseline_codes"),
        current_codes=payload.get("current_codes"),
    )
    return result.to_dict()


def analyse_windows(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Contemporaneous windows analysis over a series of dated updates.

    `compare_baselines` answers "what moved between these two". This answers
    "which period lost the time", which is the question a claim is actually
    about -- and it is a different endpoint rather than a flag on that one
    because the input is a *series*, and a series of two is the degenerate case
    rather than the normal one.
    """
    raw = payload.get("updates")
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValidationFailed("`updates` must be a list of at least two schedule updates")

    try:
        match = compare_mod.MatchKey(str(payload.get("match") or "id"))
    except ValueError as exc:
        raise ValidationFailed(f"unknown match key {payload.get('match')!r}") from exc

    updates = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise ValidationFailed(f"updates[{index}] is not an object")
        data_date = _date(entry.get("data_date"), f"updates[{index}].data_date")
        if data_date is None:
            raise ValidationFailed(f"updates[{index}] needs a `data_date`")
        cals = _calendars(entry.get("calendars") or payload.get("calendars"))
        tasks, links = _tasks_and_links(entry.get("activities") or [], cals)
        updates.append(
            windows.Update(
                data_date=data_date,
                outcome=_schedule(tasks, links, cals, entry),
                tasks=tasks,
                links=links,
                name=str(entry.get("name") or ""),
            )
        )

    try:
        return windows.analyse(updates, match=match).to_dict()
    except windows.WindowsError as exc:
        # Refused, not approximated -- and the reason travels, because "sort
        # your updates" is actionable and "invalid input" is not.
        raise ValidationFailed(str(exc)) from exc


def import_file(content: str, *, filename: str = "") -> dict[str, Any]:
    """Read an uploaded XER or MSPDI file and schedule it.

    The report says how much logic came across. Without that, a
    zero-relationship import is indistinguishable from a good one -- every
    activity reads as critical with zero float and the page looks fine.
    """
    from ..core.mspdi import MSPDIError, read_mspdi
    from ..core.xer import XERError, read_xer

    stripped = content.lstrip()
    try:
        schedule = read_mspdi(content) if stripped.startswith("<") else read_xer(content)
    except (XERError, MSPDIError) as exc:
        raise Unsupported(
            f"{filename or 'the upload'} could not be read: {exc}",
            detail="expected a Primavera .xer or an MS Project .xml (MSPDI) export",
        ) from exc

    problems = schedule.validate()
    if problems:
        raise ValidationFailed(
            f"the file describes an unschedulable network ({len(problems)} problems)",
            detail=problems[:20],
        )

    from ..core.schedule import schedule_with_network

    # `schedule_with_network` hands back the network it already built. Calling
    # `schedule()` and then `to_network()` converted every activity and
    # relationship twice to serve one response.
    outcome, net_tasks, links, _cals = schedule_with_network(schedule)
    rows = chart_rows(
        outcome,
        links,
        {a.id: (a.code, a.name) for a in schedule.activities},
        {t.id: t.kind.value for t in net_tasks},
    )

    return {
        "source": schedule.summary(),
        "has_logic": bool(schedule.relationships),
        "schedule": outcome.summary(),
        "activities": rows,
        "issues": [i.to_dict() for i in outcome.issues],
    }


def _schedule(
    tasks: list[Task],
    links: list[Link],
    calendars: Mapping[str, WorkCalendar],
    payload: Mapping[str, Any],
) -> ScheduleOutcome:
    from ..core.graph import ScheduleCycleError

    try:
        return schedule_network(
            tasks,
            links,
            calendars,
            data_date=_date(payload.get("data_date"), "data_date"),
            options=_options(payload.get("options")),
        )
    except ScheduleCycleError as exc:
        raise ValidationFailed(
            "circular logic: " + " -> ".join(exc.cycle), detail={"cycle": exc.cycle}
        ) from exc
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def exchange_from_payload(payload: Mapping[str, Any]) -> ExchangeSchedule:
    """Build a hub-model schedule from the same JSON shape. Used by the exporters."""
    calendars = _calendars(payload.get("calendars"))
    return ExchangeSchedule(
        project_name=str(payload.get("name") or "Schedule"),
        data_date=_date(payload.get("data_date"), "data_date"),
        calendars=[
            Calendar(c.id, c.name, set(c.pattern.working_weekdays), hours_per_day=c.hours_per_day)
            for c in calendars.values()
        ],
    )
