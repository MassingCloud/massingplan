"""Storing a schedule, rescheduling it, baselining it, and comparing.

The point of persistence here is not "the app has a database". It is that DCMA
checks 11 and 14 need baseline finish dates, and delay attribution needs two
schedules -- so before this layer existed neither could be reached at all.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from massingplan.core.model import (
    Calendar,
    ExchangeActivity,
    ExchangeAssignment,
    ExchangeRelationship,
    ExchangeResource,
    ExchangeSchedule,
)
from massingplan.core.network import ActivityKind, RelationType
from massingplan.models import Activity, Base, Project
from massingplan.services import projects
from massingplan.services import repository as repo

ORG = repo.DEFAULT_ORG_ID


@pytest.fixture
def session() -> Session:  # type: ignore[misc]
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as s:
        repo.ensure_default_organization(s)
        s.commit()
        yield s
    # Disposed, not left to the collector. A pooled connection that is
    # garbage collected while open is silent on 3.11 and 3.12 and a
    # ResourceWarning on 3.13 -- which, with warnings as errors, fails the
    # build on one interpreter and not the others.
    engine.dispose()


def fixture_schedule() -> ExchangeSchedule:
    """Four activities on two calendars, with a lag and a milestone."""
    return ExchangeSchedule(
        project_name="Tower",
        data_date=date(2026, 6, 1),
        planned_start=date(2026, 6, 1),
        default_calendar_id="5D",
        source_format="fixture",
        calendars=[
            Calendar("5D", "Mon-Fri", {0, 1, 2, 3, 4}, is_default=True),
            Calendar("6D", "Mon-Sat", {0, 1, 2, 3, 4, 5}),
        ],
        activities=[
            ExchangeActivity("t1", "Excavate", calendar_id="5D", duration_days=5, code="A1010"),
            ExchangeActivity("t2", "Foundations", calendar_id="5D", duration_days=10, code="A1020"),
            ExchangeActivity("t3", "Steel", calendar_id="6D", duration_days=8, code="A1030"),
            ExchangeActivity(
                "t4",
                "Topping out",
                calendar_id="5D",
                kind=ActivityKind.FINISH_MILESTONE,
                code="M1000",
            ),
        ],
        relationships=[
            ExchangeRelationship("t1", "t2"),
            ExchangeRelationship("t2", "t3", RelationType.FS, 3),
            ExchangeRelationship("t3", "t4"),
        ],
        resources=[ExchangeResource("R1", "Crew", unit="crew", max_units_per_day=1.0)],
        assignments=[ExchangeAssignment("t2", "R1", units_per_day=1.0)],
    )


def stored(session: Session) -> tuple[Project, object]:
    project, outcome, _job = projects.import_schedule(
        session, fixture_schedule(), organization_id=ORG, name="Tower", filename="tower.xer"
    )
    return project, outcome


# -- round trip ------------------------------------------------------------


def test_a_schedule_survives_the_round_trip_through_the_database(session: Session) -> None:
    """Store, reload from rows alone, reschedule -- and get the same dates.

    Reloaded from the database rather than the in-memory object, because the
    failure this catches is a field the saver writes and the loader forgets.
    """
    project, first = stored(session)
    session.expunge_all()

    reloaded = session.get(Project, project.id)
    assert reloaded is not None
    second = projects.reschedule(session, reloaded)
    assert second.to_rows() == first.to_rows()


def test_calendars_relationship_types_and_lags_all_persist(session: Session) -> None:
    project, _ = stored(session)
    by_code = {a.code: a for a in project.activities}
    assert by_code["A1030"].calendar_key == "6D"
    assert by_code["M1000"].kind is ActivityKind.FINISH_MILESTONE

    lagged = next(r for r in project.relationships_ if r.lag_days)
    assert lagged.lag_days == 3
    assert lagged.type is RelationType.FS

    calendars = {c.key: c for c in project.calendars}
    assert sorted(calendars["6D"].working_weekdays) == [0, 1, 2, 3, 4, 5]


def test_a_calendar_exception_persists_and_moves_the_finish(session: Session) -> None:
    """An unparsed shutdown is the single biggest source of date error; a
    shutdown that parses and then fails to *store* is the same bug one layer on.
    """
    from massingplan.core.model import CalendarException

    schedule = fixture_schedule()
    schedule.calendars[0].exceptions = [
        CalendarException(date(2026, 6, 8), working=False, name="Shutdown"),
        CalendarException(date(2026, 6, 9), working=False, name="Shutdown"),
    ]
    project, with_shutdown, _ = projects.import_schedule(
        session, schedule, organization_id=ORG, name="Tower with shutdown"
    )
    assert len(project.calendars[0].exceptions) == 2

    clean_project, clean, _ = projects.import_schedule(
        session, fixture_schedule(), organization_id=ORG, name="Tower clean"
    )
    assert with_shutdown.project_finish > clean.project_finish
    assert clean_project.id != project.id


def test_computed_dates_are_written_back_so_a_page_is_a_read(session: Session) -> None:
    project, outcome = stored(session)
    row = next(a for a in project.activities if a.code == "A1010")
    assert row.computed_start == date(2026, 6, 1)
    assert row.computed_finish == date(2026, 6, 5)
    assert row.is_longest_path is True
    assert row.computed_start == outcome.dates[row.id].start


def test_a_completed_activity_stores_null_float_not_zero(session: Session) -> None:
    """Zero float and "this already happened" are different statements, and
    storing 0 would put finished work on the critical path.
    """
    schedule = fixture_schedule()
    schedule.activities[0].actual_start = date(2026, 6, 1)
    schedule.activities[0].actual_finish = date(2026, 6, 5)
    schedule.data_date = date(2026, 6, 8)
    project, _outcome, _job = projects.import_schedule(
        session, schedule, organization_id=ORG, name="Progressed"
    )
    done = next(a for a in project.activities if a.code == "A1010")
    assert done.total_float_days is None
    assert done.is_critical is False


def test_reimporting_replaces_rather_than_merges(session: Session) -> None:
    """A re-import is a new statement of what the schedule is. Merging produces
    a hybrid nobody authored and nobody can reproduce from either input.
    """
    project, _ = stored(session)
    revised = fixture_schedule()
    revised.activities = revised.activities[:2]
    revised.relationships = revised.relationships[:1]

    projects.import_schedule(session, revised, organization_id=ORG, name="Tower", project=project)
    assert {a.code for a in project.activities} == {"A1010", "A1020"}
    assert session.query(Activity).filter_by(project_id=project.id).count() == 2


def test_deleting_a_project_takes_its_rows_with_it(session: Session) -> None:
    """SQLite has foreign keys off by default; without the pragma every cascade
    in the schema is decoration and this leaves orphans.
    """
    session.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys=ON"))
    project, _ = stored(session)
    project_id = project.id
    session.delete(project)
    session.flush()
    assert session.query(Activity).filter_by(project_id=project_id).count() == 0


# -- tenancy ---------------------------------------------------------------


def test_a_query_with_no_organisation_returns_nothing_rather_than_everything(
    session: Session,
) -> None:
    """Fails closed. A query returning nothing is a visible bug; one returning
    every tenant's rows is a breach that looks like a working feature.
    """
    stored(session)
    assert repo.list_projects(session, ORG)
    assert repo.list_projects(session, None) == []
    assert repo.list_projects(session, "") == []


def test_another_organisations_project_reads_as_absent(session: Session) -> None:
    """404, not 403. "This exists but is not yours" tells one contractor that
    another contractor's project id is real.
    """
    project, _ = stored(session)
    assert repo.get_project(session, project.id, ORG) is not None
    assert repo.get_project(session, project.id, "some-other-org") is None


# -- baselines: what persistence unlocks -----------------------------------


def test_dcma_checks_eleven_and_fourteen_cannot_run_without_a_baseline(
    session: Session,
) -> None:
    project, outcome = stored(session)
    report = projects.assess(session, project, outcome)
    skipped = {c["number"] for c in report["checks"] if c["status"] == "skipped"}
    assert {11, 14} <= skipped


def test_setting_a_baseline_turns_check_fourteen_on(session: Session) -> None:
    """The concrete payoff. Before persistence this check could never run."""
    schedule = fixture_schedule()
    schedule.activities[0].actual_start = date(2026, 6, 1)
    schedule.activities[0].actual_finish = date(2026, 6, 5)
    project, outcome, _job = projects.import_schedule(
        session, schedule, organization_id=ORG, name="Progressed"
    )
    projects.set_baseline(session, project, name="GMP", outcome=outcome)

    project.data_date = date(2026, 8, 1)
    later = projects.reschedule(session, project)
    report = projects.assess(session, project, later)
    check14 = next(c for c in report["checks"] if c["number"] == 14)
    assert check14["status"] != "skipped"


def test_a_baseline_is_rows_not_a_blob(session: Session) -> None:
    """You cannot join against a blob, which is why comparison was never built
    in the system this replaces.
    """
    project, outcome = stored(session)
    baseline = projects.set_baseline(session, project, name="GMP", outcome=outcome)
    assert len(baseline.rows) == len(outcome.dates)
    assert {r.code for r in baseline.rows} == {a.code for a in project.activities}
    assert baseline.project_finish == outcome.project_finish


def test_only_one_baseline_is_current(session: Session) -> None:
    project, outcome = stored(session)
    projects.set_baseline(session, project, name="GMP", outcome=outcome)
    second = projects.set_baseline(session, project, name="Recovery", outcome=outcome)
    assert sum(1 for b in project.baselines if b.is_current) == 1
    assert project.current_baseline is second


def test_a_duplicate_baseline_name_is_refused_by_name(session: Session) -> None:
    project, outcome = stored(session)
    projects.set_baseline(session, project, name="GMP", outcome=outcome)
    with pytest.raises(projects.ProjectError, match="already has a baseline named 'GMP'"):
        projects.set_baseline(session, project, name="GMP", outcome=outcome)


def test_comparing_against_a_baseline_attributes_the_slip(session: Session) -> None:
    """The whole reason the baseline is rows: two real schedules, diffed."""
    project, outcome = stored(session)
    baseline = projects.set_baseline(session, project, name="GMP", outcome=outcome)

    grown = next(a for a in project.activities if a.code == "A1020")
    grown.duration_days += 5
    later = projects.reschedule(session, project)

    result = projects.compare_to_baseline(session, project, baseline, outcome=later)
    assert result["driving_path"]["attribution_sums"] is True
    growth = [c for c in result["driving_path"]["attribution"] if c["cause"] == "duration_growth"]
    assert any(c["activity_id"] == "A1020" and c["days"] == 5 for c in growth)


def test_the_comparison_matches_on_code_so_a_reimport_does_not_break_it(
    session: Session,
) -> None:
    """A re-import renumbers every row id. Matching on them would report the
    whole schedule as removed and re-added.
    """
    project, outcome = stored(session)
    baseline = projects.set_baseline(session, project, name="GMP", outcome=outcome)
    original_ids = {a.id for a in project.activities}

    projects.import_schedule(
        session, fixture_schedule(), organization_id=ORG, name="Tower", project=project
    )
    assert {a.id for a in project.activities} != original_ids  # ids really did change

    result = projects.compare_to_baseline(session, project, baseline)
    added = [a for a in result["activities"] if "added" in a["kinds"]]
    assert added == []


def test_the_project_summary_reports_slip_as_none_without_a_baseline(
    session: Session,
) -> None:
    """`None` is not zero. No baseline is not "on time"."""
    project, outcome = stored(session)
    assert projects.summary(project, outcome)["slip_days"] is None
    projects.set_baseline(session, project, name="GMP", outcome=outcome)
    assert projects.summary(project, outcome)["slip_days"] == 0


# -- import bookkeeping ----------------------------------------------------


def test_an_import_records_whether_the_file_carried_logic(session: Session) -> None:
    """`has_logic=False` has to be visible after the fact, not only in the
    response the uploader happened to see.
    """
    _project, _outcome, job = projects.import_schedule(
        session, fixture_schedule(), organization_id=ORG, name="Tower", filename="tower.xer"
    )
    assert job.has_logic is True
    assert job.relationship_count == 3
    assert job.filename == "tower.xer"

    logic_free = fixture_schedule()
    logic_free.relationships = []
    _p, _o, job2 = projects.import_schedule(
        session, logic_free, organization_id=ORG, name="No logic"
    )
    assert job2.has_logic is False


def test_an_unschedulable_file_is_refused_before_it_is_stored(session: Session) -> None:
    """Half a project in the database is worse than a rejected upload."""
    broken = fixture_schedule()
    broken.relationships.append(ExchangeRelationship("t1", "ghost"))
    with pytest.raises(projects.ProjectError, match="cannot be stored"):
        projects.import_schedule(session, broken, organization_id=ORG, name="Broken")
    assert repo.list_projects(session, ORG) == []


def test_project_codes_are_unique_within_an_organisation(session: Session) -> None:
    """Suffixed rather than rejected: importing revision two of a file should
    not require inventing a name.
    """
    projects.import_schedule(session, fixture_schedule(), organization_id=ORG, name="Tower")
    projects.import_schedule(session, fixture_schedule(), organization_id=ORG, name="Tower")
    codes = sorted(p.code for p in repo.list_projects(session, ORG))
    assert codes == ["TOWER", "TOWER-2"]


# -- export ----------------------------------------------------------------


def test_a_stored_project_exports_back_to_xer_with_its_logic(session: Session) -> None:
    from massingplan.core.xer import read_xer, write_xer

    project, _ = stored(session)
    exported = write_xer(repo.to_exchange(project))
    reread = read_xer(exported)
    assert len(reread.relationships) == 3
    assert {a.code for a in reread.activities} == {"A1010", "A1020", "A1030", "M1000"}
    lagged = next(r for r in reread.relationships if r.lag_days)
    assert lagged.lag_days == 3


def test_the_driving_path_and_the_critical_path_can_differ_across_calendars(
    session: Session,
) -> None:
    """Not a bug, and worth pinning so nobody "fixes" it.

    In the fixture, `A1030` runs on a Mon-Sat calendar and hands off to a
    milestone on Mon-Fri. Its finish boundary lands on a Saturday -- a working
    day for it, not for the milestone -- so it can genuinely slip one working day
    without moving anything. It carries a day of float and is therefore *not*
    critical, while still being on the chain that sets the project finish.

    A single-calendar chain has neither: everything on it is both.
    """
    project, _outcome = stored(session)
    chain = next(a for a in project.activities if a.code == "A1010")
    assert chain.is_longest_path is True
    assert chain.total_float_days == 1
    assert chain.is_critical is False

    from massingplan.core.network import Link, Task
    from massingplan.core.schedule import schedule_network

    _tasks, _links, calendars, _options = repo.to_network(project)
    single = schedule_network(
        [Task("A", "", 5, "5D"), Task("B", "", 10, "5D")],
        [Link("A", "B")],
        {"5D": calendars["5D"]},
        data_date=date(2026, 6, 1),
    )
    assert all(d.total_float_days == 0 and d.is_critical for d in single.dates.values())


def test_the_outcome_reports_the_data_date_it_was_scheduled_from(session: Session) -> None:
    """Not the earliest early start.

    DCMA checks 9, 11 and 14 all compare against `outcome.data_date`. Deriving
    it as "the earliest activity start" is right only while nothing carries an
    actual -- and an actual is exactly what makes those three checks runnable.
    Getting it wrong made check 14 report *skipped* on a schedule that had a
    baseline and work due against it.
    """
    schedule = fixture_schedule()
    schedule.activities[0].actual_start = date(2026, 6, 1)
    schedule.activities[0].actual_finish = date(2026, 6, 5)
    schedule.data_date = date(2026, 7, 1)
    _project, outcome, _job = projects.import_schedule(
        session, schedule, organization_id=ORG, name="Progressed"
    )
    assert outcome.data_date == date(2026, 7, 1)
    assert outcome.project_start == date(2026, 6, 1)  # the actual, which is earlier
