"""The schema has to work on both databases, not just the one developers run.

SQLite is what the suite uses; Postgres is what production uses. The differences
that bite are silent: SQLite ignores most `ALTER`, has no native enum, has
foreign keys off by default, and returns naive datetimes from a column declared
timezone-aware. Each has already caused a bug in this repo.

The Postgres test is skipped without a `MASSINGPLAN_TEST_POSTGRES_URL`, and CI
supplies one -- so it is skipped locally and never skipped where it counts.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from massingplan.models import Base, Organization
from massingplan.models.identity import Role
from massingplan.services import accounts, projects
from massingplan.services import repository as repo
from tests.test_persistence import fixture_schedule

POSTGRES_URL = os.getenv("MASSINGPLAN_TEST_POSTGRES_URL", "")
needs_postgres = pytest.mark.skipif(
    not POSTGRES_URL, reason="set MASSINGPLAN_TEST_POSTGRES_URL to run the Postgres checks"
)


def exercise(url: str) -> None:
    """The full round trip: schema, import, baseline, compare, tenancy."""
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        org = repo.ensure_default_organization(session)
        project, outcome, job = projects.import_schedule(
            session, fixture_schedule(), organization_id=org.id, name="Tower"
        )
        assert job.has_logic is True
        assert outcome.project_finish > outcome.project_start

        baseline = projects.set_baseline(session, project, name="GMP", outcome=outcome)
        grown = next(a for a in project.activities if a.code == "A1020")
        grown.duration_days += 5
        later = projects.reschedule(session, project)
        comparison = projects.compare_to_baseline(session, project, baseline, outcome=later)
        assert comparison["driving_path"]["attribution_sums"] is True

        # Enums survive the round trip on both dialects.
        reloaded = repo.get_project(session, project.id, org.id)
        assert reloaded is not None
        assert reloaded.progress_mode.value == "retained_logic"
        assert {a.kind.value for a in reloaded.activities} >= {"task", "finish_milestone"}

        # Tenancy still fails closed.
        assert repo.get_project(session, project.id, "someone-else") is None

        # Timestamps come back timezone-aware. SQLite does not do this on its
        # own, which is what UtcDateTime is for; Postgres does, and the type
        # decorator must not break it.
        user = accounts.register(
            session,
            email="dialect@example.com",
            password="a-long-enough-passphrase",
            organization_id=org.id,
            role=Role.OWNER,
        )
        session.commit()
    with factory() as session:
        again = session.get(type(user), user.id)
        assert again is not None
        assert again.created_at.tzinfo is not None
        assert again.created_at <= datetime.now(tz=timezone.utc)
    engine.dispose()


def test_the_whole_flow_works_on_sqlite(tmp_path) -> None:  # type: ignore[no-untyped-def]
    exercise(f"sqlite:///{tmp_path / 'dialect.db'}")


@needs_postgres
def test_the_whole_flow_works_on_postgres() -> None:
    exercise(POSTGRES_URL)


@needs_postgres
def test_enums_are_varchar_with_a_check_not_a_native_type() -> None:
    """Native Postgres enums need a migration to add a member.

    The engine's enums gain members -- an eleventh constraint type should be a
    code change, not a database outage and a lock on the activities table.
    """
    engine = create_engine(POSTGRES_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    columns = {c["name"]: c for c in inspect(engine).get_columns("activities")}
    assert "VARCHAR" in str(columns["kind"]["type"]).upper()
    with engine.connect() as connection:
        native = connection.execute(
            text("SELECT count(*) FROM pg_type WHERE typtype = 'e' AND typname = 'activity_kind'")
        ).scalar()
    assert native == 0
    engine.dispose()


@needs_postgres
def test_a_date_column_is_a_date_on_postgres_too() -> None:
    """Dates are DATE, never TIMESTAMP. A datetime leaking in makes an activity
    silently unmatched in `compare()`.
    """
    engine = create_engine(POSTGRES_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    columns = {c["name"]: c for c in inspect(engine).get_columns("activities")}
    assert str(columns["computed_start"]["type"]).upper() == "DATE"
    engine.dispose()


def test_sqlite_returns_aware_datetimes_because_the_type_decorator_makes_it(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Directly, because this is the bug that broke account lock-out: SQLite
    hands back a naive datetime from a `timezone=True` column, and the
    comparison raises only after a round trip through the database.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'tz.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        org = Organization(name="TZ", slug="tz")
        session.add(org)
        session.commit()
        org_id = org.id
    with factory() as session:
        again = session.get(Organization, org_id)
        assert again is not None
        assert again.created_at.tzinfo is not None
        # The comparison that used to raise.
        assert again.created_at < datetime.now(tz=timezone.utc)
    engine.dispose()


def test_a_date_survives_sqlite_as_a_date_not_a_string(tmp_path) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine(f"sqlite:///{tmp_path / 'd.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        org = repo.ensure_default_organization(session)
        project, _outcome, _job = projects.import_schedule(
            session, fixture_schedule(), organization_id=org.id, name="Tower"
        )
        session.commit()
        project_id = project.id
    with factory() as session:
        again = repo.get_project(session, project_id, repo.DEFAULT_ORG_ID)
        assert again is not None
        assert isinstance(again.data_date, date)
        assert not isinstance(again.data_date, datetime)
    engine.dispose()


@needs_postgres
def test_the_shared_rate_limit_counter_is_atomic_on_postgres() -> None:
    """The `postgresql` branch of the upsert, which SQLite never executes.

    `DatabaseStore.hit` picks its dialect at runtime, so running the whole
    suite on SQLite leaves the Postgres statement built and never sent -- the
    same "implemented and never executed" shape that had ES256 untested. This
    is also where atomicity is a real question rather than one SQLite answers
    by serialising every writer.
    """
    from concurrent.futures import ThreadPoolExecutor

    from massingplan.services.ratelimit import DatabaseStore, Limit

    engine = create_engine(POSTGRES_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    limit = Limit(count=1000, per_seconds=60)
    now = 1_800_000_000.0
    attempts = 40

    def one(_n: int) -> int:
        return DatabaseStore(session_factory=factory).hit("auth.sign_in:pg", limit, now)[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        seen = sorted(pool.map(one, range(attempts)))

    assert seen == list(range(1, attempts + 1)), (
        f"a repeated count is a lost increment under concurrency: {seen}"
    )
    engine.dispose()


@needs_postgres
def test_the_rate_limit_window_survives_a_bigint_timestamp() -> None:
    """`window_start` holds whole seconds of wall-clock time, which is already
    past 2^31 and will keep growing. An INTEGER column would overflow on
    Postgres rather than wrapping quietly.
    """
    engine = create_engine(POSTGRES_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    columns = {c["name"]: c for c in inspect(engine).get_columns("rate_limit_hits")}
    assert "BIGINT" in str(columns["window_start"]["type"]).upper()
    engine.dispose()
