"""Takt against a stored project, and the property that ties it to the LOB page.

The takt page reads the same breakdown and the same take-off the line of
balance does. That is the design, not a shortcut: the two methods disagree
about what to *do* with the work, and must not disagree about what the work
is. If they ever do, two pages describe different buildings while claiming to
describe one, and there is no way to tell which is wrong from either page
alone.
"""

from __future__ import annotations

import io
from datetime import date

import pytest

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings
from massingplan.models import Organization, Project
from massingplan.services import accounts, projects
from massingplan.services import repository as repo

PASSWORD = "a-long-enough-passphrase"

XER = (
    "%T\tPROJECT\n%F\tproj_id\tproj_short_name\n%R\t1\t{code}\n"
    "%T\tTASK\n%F\ttask_id\tproj_id\ttask_code\ttask_name\ttarget_drtn_hr_cnt\n"
    "%R\t10\t1\tA1000\tExcavate\t40\n%E\n"
)


@pytest.fixture
def app(tmp_path):  # type: ignore[no-untyped-def]
    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / 'takt.db'}",
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
            email="planner@example.com",
            password=PASSWORD,
            organization_id=repo.DEFAULT_ORG_ID,
        )
    return application


@pytest.fixture
def client(app):  # type: ignore[no-untyped-def]
    test_client = app.test_client()
    test_client.post("/auth/sign-in", data={"email": "planner@example.com", "password": PASSWORD})
    return test_client


def _upload(client, code: str) -> str:  # type: ignore[no-untyped-def]
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(XER.format(code=code).encode()), "job.xer")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302, response.get_data(as_text=True)[:300]
    return response.headers["Location"].rstrip("/").rsplit("/", 1)[-1]


@pytest.fixture
def project_id(client) -> str:  # type: ignore[no-untyped-def]
    """Four floors, two trades: Frame at 8 crew-days a floor, Paint at 2.

    `crews` is read by the takt planner as the crew *ceiling*, which is a
    different meaning from the one line of balance gives it. Set to 4 so the
    ceiling is not what binds and the arithmetic below is about the work.
    """
    pid = _upload(client, "TOWER")
    client.post(f"/projects/{pid}/linear/locations", data={"locations": "L1\nL2\nL3\nL4"})
    client.post(
        f"/projects/{pid}/linear/trades", data={"key": "Frame", "duration_days": "8", "crews": "4"}
    )
    client.post(
        f"/projects/{pid}/linear/trades", data={"key": "Paint", "duration_days": "2", "crews": "4"}
    )
    return pid


def test_both_methods_read_the_same_work_content(app, project_id) -> None:  # type: ignore[no-untyped-def]
    """Line of balance turns work content into a duration per location; takt
    turns it into a crew count. Both start from the same number.
    """
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        linear = projects.linear_schedule(project, start=date(2026, 3, 2))
        takt = projects.takt_plan(project, takt_days=8, start=date(2026, 3, 2))

    assert linear is not None
    assert takt is not None
    lob = {(r["task_id"], r["location_id"]): r["duration_days"] for r in linear["segments"]}

    # Frame: 8 crew-days a floor. Line of balance runs it 8 days with one crew;
    # takt at 8 days needs one crew and fills the slot exactly.
    assert lob[("Frame", "L1")] == 8
    assert takt["crews"]["Frame"] == 1
    assert takt["utilisation"]["Frame"] == 1.0

    # Paint: 2 crew-days. Line of balance runs it 2 days; takt gives it a
    # full-length slot for a quarter of the work, and says so.
    assert lob[("Paint", "L1")] == 2
    assert takt["utilisation"]["Paint"] == 0.25


def test_the_duration_formula_holds_on_a_stored_project(app, project_id) -> None:  # type: ignore[no-untyped-def]
    """Two wagons, four zones, an eight-day takt: (2 + 4 - 1) x 8 = 40."""
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        takt = projects.takt_plan(project, takt_days=8, start=date(2026, 3, 2))
    assert takt is not None
    assert takt["duration_working_days"] == 40


def test_the_default_takt_is_the_shortest_feasible_one(app, project_id) -> None:
    """Not a week. A default that silently overloads the bottleneck produces a
    plan that cannot be built and looks like one that can.
    """
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        takt = projects.takt_plan(project, start=date(2026, 3, 2))
    assert takt is not None
    # Frame is 8 crew-days against a ceiling of 4 crews: two days.
    assert takt["takt_days"] == 2
    assert takt["minimum_takt_days"] == 2
    assert takt["bottleneck"] == "Frame"
    assert takt["overloaded"] == []


def test_a_longer_takt_buys_fewer_crews_with_more_idle_time(app, project_id) -> None:
    """The trade-off the method is chosen on, in both directions at once."""
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        tight = projects.takt_plan(project, takt_days=2, start=date(2026, 3, 2))
        loose = projects.takt_plan(project, takt_days=8, start=date(2026, 3, 2))

    assert tight is not None
    assert loose is not None
    assert tight["crews"]["Frame"] > loose["crews"]["Frame"]
    assert tight["duration_working_days"] < loose["duration_working_days"]
    assert tight["idle_crew_days"] < loose["idle_crew_days"]


# -- the page --------------------------------------------------------------


def test_the_takt_page_renders_the_rhythm_and_its_price(client, project_id) -> None:  # type: ignore[no-untyped-def]
    body = client.get(f"/projects/{project_id}/takt").get_data(as_text=True)
    assert "takt plan" in body
    assert "Idle capacity" in body
    assert "Frame" in body
    assert "Paint" in body
    assert "bottleneck" in body or "sets it" in body


def test_the_takt_can_be_chosen_from_the_page(client, project_id) -> None:  # type: ignore[no-untyped-def]
    loose = client.get(f"/projects/{project_id}/takt?takt_days=8").get_data(as_text=True)
    assert 'value="8"' in loose
    assert ">40<" in loose  # (2 + 4 - 1) x 8


def test_a_nonsense_takt_argument_falls_back_rather_than_five_hundreds(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """A query string is user input. `int("soon")` raises, and an unhandled
    ValueError on a GET is a 500 from something anyone can type.
    """
    response = client.get(f"/projects/{project_id}/takt?takt_days=soon")
    assert response.status_code == 200
    assert "takt plan" in response.get_data(as_text=True)


def test_a_takt_of_zero_is_refused_with_a_reason(client, project_id) -> None:  # type: ignore[no-untyped-def]
    response = client.get(f"/projects/{project_id}/takt?takt_days=0")
    assert response.status_code == 400
    assert "at least one working day" in response.get_data(as_text=True)


@pytest.mark.parametrize("absurd", ["1000000", "99999999999999999999"])
def test_an_absurd_takt_is_refused_rather_than_running_off_the_calendar(
    client, project_id, absurd
) -> None:  # type: ignore[no-untyped-def]
    """A query string is user input, and `int()` happily parses a twenty-digit
    number.

    The time axis is windowed, so it refuses immediately rather than trying to
    walk 10^20 working days -- and `TimeAxisWindowError` is a `ValueError`, so
    the route catches it. Both halves are load-bearing and neither is obvious
    from reading one file, which is why this asserts the outcome instead.
    """
    response = client.get(f"/projects/{project_id}/takt?takt_days={absurd}")
    assert response.status_code == 400, "a 500 here is reachable by typing in the URL bar"
    assert "window" in response.get_data(as_text=True).lower()


def test_a_project_with_no_breakdown_says_so_rather_than_erroring(client) -> None:  # type: ignore[no-untyped-def]
    bare = _upload(client, "BARE")
    page = client.get(f"/projects/{bare}/takt")
    assert page.status_code == 200
    assert "no location model yet" in page.get_data(as_text=True)


def test_the_workspace_links_to_it(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """A feature reachable only by typing the URL is a feature nobody uses --
    the same gap the take-off had until the form grew a box for it.
    """
    body = client.get(f"/projects/{project_id}").get_data(as_text=True)
    assert f"/projects/{project_id}/takt" in body


def test_another_tenants_takt_plan_is_a_404(app, project_id) -> None:  # type: ignore[no-untyped-def]
    """404 rather than 403: telling a stranger the project exists is the leak."""
    with database.session_scope() as session:
        rival = Organization(id="0" * 31 + "7", name="Rival", slug="rival-takt")
        session.add(rival)
        session.flush()
        accounts.register(
            session, email="rival@example.com", password=PASSWORD, organization_id=rival.id
        )

    intruder = app.test_client()
    intruder.post("/auth/sign-in", data={"email": "rival@example.com", "password": PASSWORD})
    assert intruder.get(f"/projects/{project_id}/takt").status_code == 404
