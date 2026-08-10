"""Budgets on the operations a user waits for.

Not microbenchmarks. Each of these is an operation somebody sits in front of,
with a ceiling generous enough that a busy CI runner does not fail the build and
tight enough to catch the thing that actually goes wrong: an accidental
quadratic or a query in a loop, which look fine at ten activities and take a
minute at two thousand.

**The ceilings are deliberately loose** — roughly ten times what a developer
machine does. A performance test tuned to one machine is a test that gets
skipped the first time CI is busy, and then it protects nothing. What survives a
10x margin is a complexity regression, which is the only kind worth failing a
build over.

**The two ratio tests are the real ones.** A wall-clock budget answers "is this
machine fast"; a ratio answers "is this algorithm the shape we think it is", and
that holds on any machine.

**The shapes matter more than the counts.** A chain of 2,000 is the worst case
for the forward pass; a wide fan is the worst case for any per-activity scan of
the relationship list; a network where each activity has four predecessors
across two calendars is what an imported P6 schedule actually looks like.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date

import pytest

from massingplan.core.health import assess
from massingplan.core.network import Link, RelationType, Task
from massingplan.core.schedule import schedule_network
from massingplan.core.timeaxis import STANDARD_5_DAY, WorkCalendar, WorkPattern

#: Its own CI job, and excluded from the default run by `addopts` in
#: pyproject.toml. A wall-clock ratio measured inside a full randomised suite on
#: a loaded machine fails on nothing, and a flaky timing test teaches people to
#: re-run red builds.
pytestmark = pytest.mark.performance

DATA_DATE = date(2026, 6, 1)

#: Roughly ten times a developer machine. See the module docstring.
BUDGET_SECONDS = {
    "chain_2000": 3.0,
    "fan_2000": 3.0,
    "realistic_2000": 5.0,
    "assess_2000": 5.0,
    "compare_2000": 4.0,
    "levelling_500": 8.0,
}

SIX_DAY = WorkCalendar(
    id="6d",
    name="Six day",
    pattern=WorkPattern(working_weekdays=frozenset({0, 1, 2, 3, 4, 5})),
    hours_per_day=8.0,
)
FIVE_DAY = WorkCalendar(id="5d", name="Five day", pattern=STANDARD_5_DAY, hours_per_day=8.0)
CALENDARS = {"5d": FIVE_DAY, "6d": SIX_DAY}


class Timer:
    elapsed = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.elapsed = time.perf_counter() - self._start


def fastest(call: Callable[[], object], runs: int = 3) -> float:
    """The quickest of `runs` attempts, in seconds.

    The minimum rather than the mean, because the noise here is one-sided: a
    scheduler hiccup, a GC pause or a neighbouring job can only make a sample
    slower, never faster. The mean measures the machine's mood; the minimum
    measures the code.
    """
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        call()
        best = min(best, time.perf_counter() - start)
    return best


def _chain(count: int) -> tuple[list[Task], list[Link]]:
    """A -> B -> C -> ... The worst case for the forward pass: no parallelism,
    every activity on the critical path, the longest possible driving chain.
    """
    tasks = [Task(f"A{i}", f"Activity {i}", 3) for i in range(count)]
    links = [Link(f"A{i - 1}", f"A{i}", RelationType.FS) for i in range(1, count)]
    return tasks, links


def _fan(count: int) -> tuple[list[Task], list[Link]]:
    """One predecessor, `count` successors, one join.

    The worst case for any per-activity scan of the relationship list: with
    4,000 links and 2,002 activities, a scan per activity is eight million
    comparisons and takes seconds, where an index takes milliseconds.
    """
    tasks = [Task("START", "Start", 1)]
    tasks += [Task(f"A{i}", f"Activity {i}", 3) for i in range(count)]
    tasks.append(Task("END", "End", 1))
    links = [Link("START", f"A{i}", RelationType.FS) for i in range(count)]
    links += [Link(f"A{i}", "END", RelationType.FS) for i in range(count)]
    return tasks, links


def _realistic(count: int) -> tuple[list[Task], list[Link]]:
    """What an imported P6 schedule looks like: several predecessors each, a mix
    of relationship types, lags, and more than one calendar.
    """
    kinds = (RelationType.FS, RelationType.SS, RelationType.FF, RelationType.FS)
    tasks: list[Task] = []
    links: list[Link] = []
    for i in range(count):
        tasks.append(
            Task(
                f"A{i}",
                f"Activity {i}",
                (i % 15) + 1,
                calendar_id="6d" if i % 7 == 0 else "5d",
            )
        )
        for offset in (1, 3, 11, 37):
            source = i - offset
            if source >= 0:
                links.append(
                    Link(f"A{source}", f"A{i}", kinds[offset % len(kinds)], lag_days=offset % 3)
                )
    return tasks, links


# -- the forward and backward passes ---------------------------------------


def test_a_2000_activity_chain_schedules_quickly() -> None:
    tasks, links = _chain(2000)
    with Timer() as timer:
        outcome = schedule_network(tasks, links, data_date=DATA_DATE)
    assert len(outcome.to_rows()) == 2000
    assert timer.elapsed < BUDGET_SECONDS["chain_2000"], (
        f"a 2,000-activity chain took {timer.elapsed:.2f}s. Both passes are "
        "linear in activities plus links; this is what a quadratic one costs."
    )


def test_a_2000_way_fan_schedules_quickly() -> None:
    tasks, links = _fan(2000)
    with Timer() as timer:
        outcome = schedule_network(tasks, links, data_date=DATA_DATE)
    assert outcome.project_finish is not None
    assert timer.elapsed < BUDGET_SECONDS["fan_2000"], (
        f"a 2,000-way fan took {timer.elapsed:.2f}s -- look for a scan over "
        "links where an index belongs"
    )


def test_a_realistic_multi_calendar_network_schedules_quickly() -> None:
    tasks, links = _realistic(2000)
    with Timer() as timer:
        outcome = schedule_network(tasks, links, CALENDARS, data_date=DATA_DATE)
    assert len(outcome.to_rows()) == 2000
    assert timer.elapsed < BUDGET_SECONDS["realistic_2000"], (
        f"a realistic 2,000-activity network took {timer.elapsed:.2f}s. Each "
        "calendar's lattice is built once and shared -- check nothing rebuilt "
        "it per activity."
    )


def test_scheduling_scales_about_linearly() -> None:
    """The shape, not the number.

    Ten times the activities must not be a hundred times the time, and a ratio
    catches that on any machine where a wall-clock budget cannot.
    """
    schedule_network(*_chain(50), data_date=DATA_DATE)  # warm anything lazy

    small_tasks, small_links = _chain(200)
    big_tasks, big_links = _chain(2000)
    small = fastest(lambda: schedule_network(small_tasks, small_links, data_date=DATA_DATE))
    big = fastest(lambda: schedule_network(big_tasks, big_links, data_date=DATA_DATE))

    # Linear would be ~10x, quadratic ~100x. A ceiling of 30x leaves room for
    # constant overheads dominating the small case without letting a quadratic
    # through.
    ratio = big / max(small, 1e-6)
    assert ratio < 30, (
        f"10x the activities took {ratio:.0f}x the time "
        f"({small * 1000:.0f}ms then {big * 1000:.0f}ms). That is the signature "
        "of a quadratic pass, not a slow machine."
    )


def test_the_driving_path_walk_does_not_blow_up_on_a_long_chain() -> None:
    """The driving path is recovered by walking back through recorded drivers.
    A walk that re-searched at each step would be quadratic in the chain, and a
    2,000-long chain is entirely driving.
    """
    tasks, links = _chain(2000)
    with Timer() as timer:
        outcome = schedule_network(tasks, links, data_date=DATA_DATE)
    assert len(outcome.longest_path) == 2000, "the driving path lost activities on a pure chain"
    assert timer.elapsed < BUDGET_SECONDS["chain_2000"], (
        f"scheduling plus the driving-path walk took {timer.elapsed:.2f}s"
    )


# -- the analysis on top ---------------------------------------------------


def test_the_dcma_assessment_is_quick_on_2000_activities() -> None:
    """Fourteen checks, several of which are naturally quadratic if written
    carelessly -- "how many activities have no predecessor", over a list, is a
    scan per activity.
    """
    tasks, links = _realistic(2000)
    outcome = schedule_network(tasks, links, CALENDARS, data_date=DATA_DATE)
    with Timer() as timer:
        report = assess(outcome, tasks, links, CALENDARS)
    assert len(report.assessed) + len(report.skipped) == 14
    assert timer.elapsed < BUDGET_SECONDS["assess_2000"], (
        f"the DCMA assessment took {timer.elapsed:.2f}s on 2,000 activities"
    )


def test_baseline_comparison_is_quick_on_2000_activities() -> None:
    from massingplan.core.compare import MatchKey, compare

    tasks, links = _chain(2000)
    before = schedule_network(tasks, links, data_date=DATA_DATE)
    # One activity in the middle grows: the realistic case, where most match and
    # a slice of them moved.
    moved = [Task(t.id, t.name, t.duration_days + (5 if t.id == "A900" else 0)) for t in tasks]
    after = schedule_network(moved, links, data_date=DATA_DATE)

    with Timer() as timer:
        result = compare(
            before,
            after,
            baseline_network=(tasks, links),
            current_network=(moved, links),
            match=MatchKey.ID,
        )
    assert result is not None
    assert timer.elapsed < BUDGET_SECONDS["compare_2000"], (
        f"comparing two 2,000-activity schedules took {timer.elapsed:.2f}s. "
        "Matching is by dictionary; a nested loop over both sides is four "
        "million comparisons."
    )


def test_the_monte_carlo_cost_is_proportional_to_iterations() -> None:
    """Risk is the one operation that is honestly slow, and the docs say so. What
    must hold is that it is slow *linearly* in iterations -- if the calendar
    lattices were rebound inside the loop instead of before it, doubling the
    iterations would more than double the time.
    """
    from massingplan.core.risk import simulate

    tasks, links = _chain(200)
    few = fastest(lambda: simulate(tasks, links, data_date=DATA_DATE, iterations=100), runs=2)
    many = fastest(lambda: simulate(tasks, links, data_date=DATA_DATE, iterations=400), runs=2)
    ratio = many / max(few, 1e-6)
    assert ratio < 8, (
        f"4x the iterations took {ratio:.1f}x the time. Something per-run is "
        "being redone per iteration -- most likely a calendar lattice."
    )


# -- levelling, the genuinely expensive one --------------------------------


def test_levelling_500_activities_stays_predictable() -> None:
    """A far smaller network on purpose. Serial SGS is the one operation here
    expected to be slow; the budget exists so it stays *predictably* slow rather
    than becoming unusable without anybody noticing.
    """
    from massingplan.core.levelling import LevellingRequest, level
    from massingplan.core.resources import Demand, ResourceAvailability

    count = 500
    tasks = [Task(f"A{i}", f"Activity {i}", (i % 7) + 1) for i in range(count)]
    links = [Link(f"A{i - 1}", f"A{i}", RelationType.FS) for i in range(1, count, 5)]
    outcome = schedule_network(tasks, links, data_date=DATA_DATE)

    with Timer() as timer:
        result = level(
            LevellingRequest(
                outcome=outcome,
                tasks=tasks,
                links=links,
                calendars={},
                demands=[Demand(f"A{i}", "crew", 1.0) for i in range(count)],
                availability=[ResourceAvailability("crew", 4.0)],
            )
        )
    assert result is not None
    assert timer.elapsed < BUDGET_SECONDS["levelling_500"], (
        f"levelling 500 activities took {timer.elapsed:.2f}s"
    )


# -- the pages, not just the engine ----------------------------------------


@pytest.fixture
def app(tmp_path):  # type: ignore[no-untyped-def]
    from massingplan import database
    from massingplan.app import create_app
    from massingplan.config import Settings
    from massingplan.services import accounts
    from massingplan.services import repository as repo

    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / 'perf.db'}",
            rate_limit_enabled=False,
        )
    )
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    database.create_all()
    with database.session_scope() as session:
        repo.ensure_default_organization(session)
        accounts.register(
            session,
            email="perf@example.com",
            password="a-long-enough-passphrase",
            organization_id=repo.DEFAULT_ORG_ID,
        )
    return application


def _upload(client, code: str, activities: int = 60) -> None:
    import io

    rows = "\n".join(
        f"%R\t{i}\t1\tA{i:04d}\tActivity {i}\t{((i % 10) + 1) * 8}" for i in range(activities)
    )
    xer = (
        f"%T\tPROJECT\n%F\tproj_id\tproj_short_name\n%R\t1\t{code}\n"
        "%T\tTASK\n%F\ttask_id\tproj_id\ttask_code\ttask_name\ttarget_drtn_hr_cnt\n"
        f"{rows}\n%E\n"
    )
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(xer.encode()), f"{code}.xer")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302, response.get_data(as_text=True)[:300]


def test_the_project_list_does_not_reschedule_every_project(app) -> None:
    """The regression this exists for.

    The list page once ran a full CPM per row, so twenty projects meant twenty
    schedules and the page got slower with every project anybody added. It reads
    denormalised columns now.

    Asserted as a ratio between one project and twenty, because that is the
    property: listing must be roughly flat in project count.
    """
    client = app.test_client()
    client.post(
        "/auth/sign-in",
        data={"email": "perf@example.com", "password": "a-long-enough-passphrase"},
    )

    _upload(client, "P000")
    client.get("/projects")  # warm
    one = fastest(lambda: client.get("/projects"), runs=5)

    for index in range(1, 20):
        _upload(client, f"P{index:03d}")
    assert client.get("/projects").status_code == 200
    twenty = fastest(lambda: client.get("/projects"), runs=5)

    ratio = twenty / max(one, 1e-4)
    assert ratio < 4, (
        f"listing 20 projects took {ratio:.1f}x listing one "
        f"({twenty * 1000:.0f}ms vs {one * 1000:.0f}ms). The page "
        "reads six denormalised columns on `projects` and loads no children; "
        "something has started touching a relationship again -- check "
        "`repository.list_projects` still passes `noload` and "
        "`projects.stored_summary` still reads only columns."
    )


def test_importing_a_1000_activity_file_is_not_a_per_row_query(app) -> None:
    """Import writes activities and relationships in bulk. A `session.get()` per
    row to check for an existing one is the classic version of this, and it
    turns a two-second import into a two-minute one at file sizes people
    actually have.
    """
    client = app.test_client()
    client.post(
        "/auth/sign-in",
        data={"email": "perf@example.com", "password": "a-long-enough-passphrase"},
    )

    _upload(client, "SMALL", activities=100)
    # A fresh code each time -- an import is a write, so it cannot be repeated
    # against the same project without hitting the unique constraint.
    counter = iter(range(100))
    small = fastest(lambda: _upload(client, f"S{next(counter):03d}", activities=100), runs=3)
    big = fastest(lambda: _upload(client, f"B{next(counter):03d}", activities=1000), runs=2)

    ratio = big / max(small, 1e-4)
    assert ratio < 40, (
        f"10x the rows took {ratio:.0f}x the time "
        f"({small * 1000:.0f}ms then {big * 1000:.0f}ms). Look for a query "
        "inside the row loop."
    )
