"""Storing a location breakdown, and getting the same answer back.

The engine was testable in memory from the day it was written. What this covers
is the half that turns it from a demo into a tool: a breakdown that survives a
round trip through the database and schedules to the same dates the in-memory
model does. If those two ever disagree, the stored model is a different schedule
wearing the same name.
"""

from __future__ import annotations

import io
from datetime import date

import pytest
from sqlalchemy import select

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings
from massingplan.core.locations import LinearTask, Location, compute
from massingplan.models import LinearActivity, LinearQuantity, Project, ProjectLocation
from massingplan.services import accounts, projects
from massingplan.services import repository as repo

PASSWORD = "a-long-enough-passphrase"
OTHER_ORG = "00000000000000000000000000000042"

XER = (
    "%T\tPROJECT\n%F\tproj_id\tproj_short_name\n%R\t1\t{code}\n"
    "%T\tTASK\n%F\ttask_id\tproj_id\ttask_code\ttask_name\ttarget_drtn_hr_cnt\n"
    "%R\t10\t1\tA1000\tExcavate\t40\n%E\n"
)

FLOORS = [("Level 1", "Ground"), ("Level 2", ""), ("Level 3", ""), ("Level 4", "")]


@pytest.fixture
def app(tmp_path):  # type: ignore[no-untyped-def]
    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / 'loc.db'}",
            rate_limit_enabled=False,
        )
    )
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    database.create_all()
    with database.session_scope() as session:
        repo.ensure_default_organization(session)
        from massingplan.models import Organization

        session.add(Organization(id=OTHER_ORG, name="Rival", slug="rival-loc"))
        session.flush()
        accounts.register(
            session,
            email="planner@example.com",
            password=PASSWORD,
            organization_id=repo.DEFAULT_ORG_ID,
        )
    return application


def _client(app):  # type: ignore[no-untyped-def]
    client = app.test_client()
    client.post("/auth/sign-in", data={"email": "planner@example.com", "password": PASSWORD})
    return client


def _project(client, code: str = "TOWER") -> str:
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(XER.format(code=code).encode()), "job.xer")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302, response.get_data(as_text=True)[:300]
    return response.headers["Location"].rstrip("/").rsplit("/", 1)[-1]


def _with_model(session, project: Project) -> None:
    """A four-floor breakdown and two trades: framing slow, painting fast."""
    repo.replace_locations(session, project, FLOORS)
    repo.upsert_linear_activity(session, project, key="Frame", duration_days=4)
    repo.upsert_linear_activity(session, project, key="Paint", duration_days=1, buffer_days=0)


# -- the round trip --------------------------------------------------------


def test_a_breakdown_survives_the_database(app) -> None:  # type: ignore[no-untyped-def]
    project_id = _project(_client(app))
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        _with_model(session, project)
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert [loc.key for loc in project.locations] == [key for key, _ in FLOORS]
        assert project.locations[0].name == "Ground"
        assert [t.key for t in project.linear_activities] == ["Frame", "Paint"]


def test_the_stored_model_schedules_to_the_same_dates_as_the_in_memory_one(app) -> None:  # type: ignore[no-untyped-def]
    """The point of persistence: storing the model must not change the answer.

    The same four floors and two trades, computed once from objects built by
    hand and once from rows loaded back out. If these diverge, the stored model
    is a different schedule with the same name.
    """
    project_id = _project(_client(app))
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        _with_model(session, project)
        session.commit()

    in_memory = compute(
        [
            LinearTask(id="Frame", name="Frame", duration_days=4),
            LinearTask(id="Paint", name="Paint", duration_days=1),
        ],
        [Location(key, name, sequence=i) for i, (key, name) in enumerate(FLOORS)],
    )

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        tasks, locations = repo.to_linear(project)
        stored = compute(tasks, locations)

    assert [(s.task_id, s.location_id, s.start_offset) for s in stored.segments] == [
        (s.task_id, s.location_id, s.start_offset) for s in in_memory.segments
    ]
    assert stored.continuity_cost_days == in_memory.continuity_cost_days
    assert stored.duration_days == in_memory.duration_days


def test_sequence_survives_and_is_not_the_row_order(app) -> None:  # type: ignore[no-untyped-def]
    """Flow direction is the stored `sequence`, so it has to come back in that
    order regardless of how the rows happen to be returned.
    """
    project_id = _project(_client(app))
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        repo.replace_locations(session, project, FLOORS)
        # Shuffle the sequences: the flow is now the reverse of insertion order.
        for index, location in enumerate(project.locations):
            location.sequence = len(FLOORS) - index
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        _tasks, locations = repo.to_linear(project)
        assert [loc.id for loc in locations] == [key for key, _ in reversed(FLOORS)]


# -- replacement and cascade ----------------------------------------------


def test_replacing_the_breakdown_replaces_it_rather_than_merging(app) -> None:  # type: ignore[no-untyped-def]
    """A breakdown is a statement of what the building is. Merging two of them
    produces a floor list nobody authored.
    """
    project_id = _project(_client(app))
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        repo.replace_locations(session, project, FLOORS)
        session.commit()
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        repo.replace_locations(session, project, [("Zone A", ""), ("Zone B", "")])
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert [loc.key for loc in project.locations] == ["Zone A", "Zone B"]
        assert session.scalars(select(ProjectLocation)).all() == list(project.locations)


def test_a_quantity_goes_with_the_location_it_belonged_to(app) -> None:  # type: ignore[no-untyped-def]
    """The reason quantities are rows with a foreign key rather than keys in a
    JSON blob: a quantity against a level that no longer exists is not data
    worth keeping, and a blob would have silently kept it.
    """
    project_id = _project(_client(app))
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        repo.replace_locations(session, project, FLOORS)
        repo.upsert_linear_activity(
            session, project, key="Slab", rate=95.0, quantities={"Level 1": 380.0}
        )
        session.commit()
        assert session.scalars(select(LinearQuantity)).all()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        repo.replace_locations(session, project, [("Zone A", "")])
        session.commit()

    with database.session_scope() as session:
        assert session.scalars(select(LinearQuantity)).all() == []


def test_deleting_the_project_takes_the_location_model_with_it(app) -> None:  # type: ignore[no-untyped-def]
    project_id = _project(_client(app))
    with database.session_scope() as session:
        session.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys=ON"))
        project = session.get(Project, project_id)
        assert project is not None
        _with_model(session, project)
        session.commit()

    client = _client(app)
    assert client.post(f"/projects/{project_id}/delete").status_code == 302

    with database.session_scope() as session:
        assert session.scalars(select(ProjectLocation)).all() == []
        assert session.scalars(select(LinearActivity)).all() == []


def test_re_entering_a_trade_updates_it_rather_than_duplicating(app) -> None:  # type: ignore[no-untyped-def]
    project_id = _project(_client(app))
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        repo.replace_locations(session, project, FLOORS)
        repo.upsert_linear_activity(session, project, key="Frame", duration_days=4)
        repo.upsert_linear_activity(session, project, key="Frame", duration_days=6)
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert len(project.linear_activities) == 1
        assert project.linear_activities[0].duration_days == 6


# -- the service -----------------------------------------------------------


def test_a_project_with_no_breakdown_reports_none_rather_than_an_empty_chart(app) -> None:  # type: ignore[no-untyped-def]
    """ "No location model" and "a model that schedules to nothing" are different
    states, and the page says something different for each.
    """
    project_id = _project(_client(app))
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert projects.linear_schedule(project) is None


def test_the_service_returns_the_shape_the_template_and_api_share(app) -> None:  # type: ignore[no-untyped-def]
    project_id = _project(_client(app))
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        _with_model(session, project)
        session.commit()
        result = projects.linear_schedule(project, start=date(2026, 6, 1))

    assert result is not None
    assert set(result) >= {
        "start",
        "duration_working_days",
        "segments",
        "interferences",
        "continuity_cost_days",
        "issues",
        "activities",
    }
    assert len(result["segments"]) == 8
    # Paint is the faster trade, so it is held by the *top* floor.
    assert result["interferences"][0]["location_id"] == "Level 4"
    assert result["interferences"][0]["converging"] is True


# -- through the pages -----------------------------------------------------


def test_the_page_offers_the_form_when_there_is_no_model(app) -> None:  # type: ignore[no-untyped-def]
    client = _client(app)
    project_id = _project(client)
    body = client.get(f"/projects/{project_id}/linear").get_data(as_text=True)
    assert "no location model yet" in body
    assert "Location breakdown" in body


def test_a_breakdown_can_be_entered_and_the_chart_appears(app) -> None:  # type: ignore[no-untyped-def]
    """End to end, the way a planner does it: paste the floors, add two trades,
    see the line of balance.
    """
    client = _client(app)
    project_id = _project(client)

    assert (
        client.post(
            f"/projects/{project_id}/linear/locations",
            data={"locations": "Level 1 | Ground\nLevel 2\nLevel 3\nLevel 4"},
        ).status_code
        == 302
    )
    for key, days in (("Frame", 4), ("Paint", 1)):
        assert (
            client.post(
                f"/projects/{project_id}/linear/trades",
                data={"key": key, "duration_days": days},
            ).status_code
            == 302
        )

    body = client.get(f"/projects/{project_id}/linear").get_data(as_text=True)
    assert "lob-host" in body
    assert "data-segments" in body
    assert "Level 4" in body


def test_a_duplicate_location_key_is_refused_with_a_reason(app) -> None:  # type: ignore[no-untyped-def]
    client = _client(app)
    project_id = _project(client)
    response = client.post(
        f"/projects/{project_id}/linear/locations",
        data={"locations": "Level 1\nLevel 1"},
    )
    assert response.status_code == 400
    assert "share a key" in response.get_data(as_text=True)


def test_a_trade_can_be_removed(app) -> None:  # type: ignore[no-untyped-def]
    client = _client(app)
    project_id = _project(client)
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1\nL2"})
    client.post(f"/projects/{project_id}/linear/trades", data={"key": "Frame"})
    assert client.post(f"/projects/{project_id}/linear/trades/Frame/delete").status_code == 302

    with database.session_scope() as session:
        assert session.scalars(select(LinearActivity)).all() == []


def test_another_tenants_location_model_is_a_404(app) -> None:  # type: ignore[no-untyped-def]
    """Same rule as everywhere else: 403 would confirm the project id is real."""
    client = _client(app)
    project_id = _project(client)
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        project.organization_id = OTHER_ORG
        session.commit()

    assert client.get(f"/projects/{project_id}/linear").status_code == 404
    assert (
        client.post(
            f"/projects/{project_id}/linear/locations", data={"locations": "L1"}
        ).status_code
        == 404
    )
    assert (
        client.post(f"/projects/{project_id}/linear/trades", data={"key": "X"}).status_code == 404
    )


def test_a_viewer_cannot_edit_the_location_model(app) -> None:  # type: ignore[no-untyped-def]
    """Reading the chart is not the same as redefining the building."""
    from massingplan.models.identity import Role

    with database.session_scope() as session:
        user = accounts.register(
            session,
            email="viewer@example.com",
            password=PASSWORD,
            organization_id=repo.DEFAULT_ORG_ID,
        )
        membership = user.membership_in(repo.DEFAULT_ORG_ID)
        assert membership is not None
        membership.role = Role.VIEWER

    project_id = _project(_client(app))
    viewer = app.test_client()
    viewer.post("/auth/sign-in", data={"email": "viewer@example.com", "password": PASSWORD})

    assert viewer.get(f"/projects/{project_id}/linear").status_code == 200
    assert (
        viewer.post(
            f"/projects/{project_id}/linear/locations", data={"locations": "L1"}
        ).status_code
        == 403
    )


def test_the_demo_page_no_longer_claims_locations_are_unstored() -> None:
    """That docstring was true when it was written and false the moment the
    tables landed. Stale claims are the defect this repo keeps re-finding.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "massingplan" / "blueprints" / "main.py"
    ).read_text(encoding="utf-8")
    assert "locations are not persisted yet" not in source
