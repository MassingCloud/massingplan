"""Last Planner against a stored project.

The engine's rules are tested in `test_lastplanner.py`. What this covers is
that the stored path enforces *the same* rules rather than a second copy of
them -- a route that let a constrained commitment through, or defaulted a
variance reason, would leave the engine's guarantees true and the product's
false.

The property at the centre of it: **the denominator is frozen**. A commitment
recorded for a week stays in that week's PPC whatever happens to it afterwards,
because shrinking the denominator after seeing how the week went is what turns
PPC into a number that only ever goes up.
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings
from massingplan.core.lastplanner import ConstraintKind, LastPlannerError, VarianceReason
from massingplan.models import Organization, Project
from massingplan.services import accounts, production
from massingplan.services import repository as repo

PASSWORD = "a-long-enough-passphrase"
MONDAY = date(2026, 3, 2)

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
            database_url=f"sqlite:///{tmp_path / 'lp.db'}",
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


@pytest.fixture
def project_id(client) -> str:  # type: ignore[no-untyped-def]
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(XER.format(code="TOWER").encode()), "job.xer")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    return response.headers["Location"].rstrip("/").rsplit("/", 1)[-1]


# -- the week ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 3, 2), date(2026, 3, 2)),  # a Monday is its own
        (date(2026, 3, 4), date(2026, 3, 2)),  # midweek
        (date(2026, 3, 8), date(2026, 3, 2)),  # Sunday belongs to the week before
        (date(2026, 3, 9), date(2026, 3, 9)),  # the next Monday
    ],
)
def test_the_week_is_derived_in_exactly_one_place(day: date, expected: date) -> None:
    """Two functions computing "the start of the week" is two chances for one
    of them to use Sunday, and the symptom is a plan quietly in the wrong week.
    """
    assert production.monday_of(day) == expected


def test_a_week_can_only_be_opened_on_a_monday(app, project_id) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        with pytest.raises(LastPlannerError, match="Monday"):
            production.open_week(session, project, date(2026, 3, 4))


def test_opening_the_same_week_twice_returns_the_same_plan(app, project_id) -> None:  # type: ignore[no-untyped-def]
    """Two plans for one week is two denominators for one PPC."""
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        first = production.open_week(session, project, MONDAY)
        second = production.open_week(session, project, MONDAY)
        assert first.id == second.id
        assert len(project.weekly_plans) == 1


# -- committing -------------------------------------------------------------


def test_a_make_ready_commitment_is_stored(app, project_id) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        plan = production.open_week(session, project, MONDAY)
        production.add_commitment(
            session, plan, description="Frame level 3", crew="Steel gang", activity_code="A1000"
        )
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        stored = project.weekly_plans[0].commitments[0]
        assert stored.description == "Frame level 3"
        assert stored.completed is None, "a new commitment is unassessed, not failed"


def test_a_constrained_commitment_is_refused_and_stores_nothing(app, project_id) -> None:  # type: ignore[no-untyped-def]
    """The rule is the engine's, enforced on the stored path. A route that
    merely warned would be used to commit constrained work every week.
    """
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        plan = production.open_week(session, project, MONDAY)
        with pytest.raises(LastPlannerError, match="not make-ready"):
            production.add_commitment(
                session,
                plan,
                description="Frame level 3",
                crew="Steel gang",
                constraints=[
                    {
                        "kind": ConstraintKind.MATERIALS.value,
                        "description": "steel delivery",
                        "owner": "procurement",
                        "promised_by": MONDAY,
                    }
                ],
            )
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert project.weekly_plans[0].commitments == []


def test_a_constraint_cleared_before_the_week_lets_the_work_be_promised(app, project_id) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        plan = production.open_week(session, project, MONDAY)
        production.add_commitment(
            session,
            plan,
            description="Frame level 3",
            crew="Steel gang",
            constraints=[
                {
                    "kind": ConstraintKind.MATERIALS.value,
                    "description": "steel delivery",
                    "owner": "procurement",
                    "promised_by": MONDAY - timedelta(days=7),
                    "removed_on": MONDAY - timedelta(days=1),
                }
            ],
        )
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert len(project.weekly_plans[0].commitments) == 1


def test_a_constraint_cleared_after_the_week_starts_still_blocks_it(app, project_id) -> None:  # type: ignore[no-untyped-def]
    """ "Cleared next Friday" must not make this Monday's plan look ready --
    that plan is exactly the one that then fails.
    """
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        plan = production.open_week(session, project, MONDAY)
        with pytest.raises(LastPlannerError, match="not make-ready"):
            production.add_commitment(
                session,
                plan,
                description="Frame level 3",
                crew="Steel gang",
                constraints=[
                    {
                        "kind": ConstraintKind.MATERIALS.value,
                        "description": "steel delivery",
                        "owner": "procurement",
                        "promised_by": MONDAY,
                        "removed_on": MONDAY + timedelta(days=4),
                    }
                ],
            )


# -- assessment and PPC -----------------------------------------------------


def _week_with(session, project, met: int, missed: int) -> None:  # type: ignore[no-untyped-def]
    plan = production.open_week(session, project, MONDAY)
    for n in range(met):
        row = production.add_commitment(session, plan, description=f"met {n}", crew="Steel gang")
        production.assess_commitment(session, row, completed=True)
    for n in range(missed):
        row = production.add_commitment(session, plan, description=f"missed {n}", crew="Steel gang")
        production.assess_commitment(
            session, row, completed=False, reason=VarianceReason.MATERIALS.value
        )


def test_ppc_comes_out_of_the_stored_week(app, project_id) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        _week_with(session, project, met=3, missed=1)
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        report = production.reliability(project)
    assert report is not None
    assert report["weeks"][0]["ppc"] == 0.75
    assert report["weeks"][0]["committed"] == 4


def test_an_unassessed_commitment_makes_the_stored_week_unmeasurable(app, project_id) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        _week_with(session, project, met=3, missed=0)
        plan = production.open_week(session, project, MONDAY)
        production.add_commitment(session, plan, description="not yet judged", crew="Steel gang")
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        report = production.reliability(project)
    assert report is not None
    assert report["weeks"][0]["ppc"] is None
    assert report["mean_ppc"] is None


def test_a_missed_commitment_with_no_reason_is_recorded_as_not_recorded(app, project_id) -> None:  # type: ignore[no-untyped-def]
    """The route may not leave it blank -- a missed commitment with no reason
    is the one thing this whole method exists to collect. `not_recorded` is a
    *named* reason so the count of unexplained failures is itself reportable.
    """
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        plan = production.open_week(session, project, MONDAY)
        row = production.add_commitment(session, plan, description="frame", crew="Steel gang")
        production.assess_commitment(session, row, completed=False, reason=None)
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        stored = project.weekly_plans[0].commitments[0]
        assert stored.reason == VarianceReason.NOT_RECORDED.value
        report = production.reliability(project)
    assert report is not None
    assert report["weeks"][0]["variance"] == {"not_recorded": 1}


def test_a_project_with_no_plans_reports_none_rather_than_an_empty_report(app, project_id) -> None:  # type: ignore[no-untyped-def]
    """ "Does not use Last Planner" and "has planned nothing" are different
    states, and the page says something different for each.
    """
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert production.reliability(project) is None


# -- the constraint log -----------------------------------------------------


def test_the_open_constraint_log_puts_the_oldest_broken_promise_first(app, project_id) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        plan = production.open_week(session, project, MONDAY)
        for days, label in ((2, "recent"), (30, "ancient"), (10, "middling")):
            production.add_commitment(
                session,
                plan,
                description=f"blocked by {label}",
                crew="Steel gang",
                constraints=[
                    {
                        "kind": ConstraintKind.DESIGN.value,
                        "description": label,
                        "owner": "design",
                        "promised_by": MONDAY - timedelta(days=days),
                    }
                ],
                allow_constrained=True,
            )
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        log = production.open_constraints(project, on=MONDAY)
    assert [row["description"] for row in log] == ["ancient", "middling", "recent"]
    assert log[0]["overdue_days"] == 30
    assert log[0]["owner"] == "design"
    assert log[0]["blocks"] == "blocked by ancient"


def test_a_cleared_constraint_leaves_the_log(app, project_id) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        plan = production.open_week(session, project, MONDAY)
        row = production.add_commitment(
            session,
            plan,
            description="frame",
            crew="Steel gang",
            constraints=[
                {
                    "kind": ConstraintKind.MATERIALS.value,
                    "description": "steel",
                    "owner": "procurement",
                    "promised_by": MONDAY,
                }
            ],
            allow_constrained=True,
        )
        assert len(production.open_constraints(project, on=MONDAY)) == 1
        production.clear_constraint(session, row.constraints[0], removed_on=MONDAY)
        session.commit()

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert production.open_constraints(project, on=MONDAY + timedelta(days=1)) == []


# -- the page ---------------------------------------------------------------


def test_the_page_renders_and_the_workspace_links_to_it(client, project_id) -> None:  # type: ignore[no-untyped-def]
    body = client.get(f"/projects/{project_id}/production").get_data(as_text=True)
    assert "production control" in body
    assert "Mean PPC" not in body, "no plans yet, so no reliability to show"

    workspace = client.get(f"/projects/{project_id}").get_data(as_text=True)
    assert f"/projects/{project_id}/production" in workspace


def test_a_commitment_can_be_made_and_assessed_from_the_page(client, project_id) -> None:  # type: ignore[no-untyped-def]
    week = MONDAY.isoformat()
    response = client.post(
        f"/projects/{project_id}/production/commit?week={week}",
        data={"description": "Frame level 3", "crew": "Steel gang"},
    )
    assert response.status_code == 302, response.get_data(as_text=True)[:400]

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        commitment_id = project.weekly_plans[0].commitments[0].id

    assert (
        client.post(
            f"/projects/{project_id}/production/{commitment_id}/assess?week={week}",
            data={"completed": "yes"},
        ).status_code
        == 302
    )
    body = client.get(f"/projects/{project_id}/production?week={week}").get_data(as_text=True)
    assert "100%" in body


def test_the_page_refuses_a_constrained_commitment_with_a_reason(client, project_id) -> None:  # type: ignore[no-untyped-def]
    response = client.post(
        f"/projects/{project_id}/production/commit?week={MONDAY.isoformat()}",
        data={
            "description": "Frame level 3",
            "crew": "Steel gang",
            "constraint_kind": ConstraintKind.MATERIALS.value,
            "constraint_description": "steel delivery",
            "constraint_owner": "procurement",
            "constraint_promised_by": MONDAY.isoformat(),
        },
    )
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "not make-ready" in body
    assert "Frame level 3" in body, "the rejected form is handed back"


def test_a_week_in_the_query_string_is_snapped_to_its_monday(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """Somebody linking to a Wednesday means that week. A 400 would be
    pedantry; silently using the Wednesday would put the plan in no week.
    """
    body = client.get(f"/projects/{project_id}/production?week=2026-03-04").get_data(as_text=True)
    assert "2026-03-02" in body


def test_an_unparseable_week_falls_back_rather_than_five_hundreds(client, project_id) -> None:  # type: ignore[no-untyped-def]
    response = client.get(f"/projects/{project_id}/production?week=next-tuesday")
    assert response.status_code == 200


def test_another_tenants_production_board_is_a_404(app, project_id) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        rival = Organization(id="0" * 31 + "5", name="Rival", slug="rival-lp")
        session.add(rival)
        session.flush()
        accounts.register(
            session, email="rival@example.com", password=PASSWORD, organization_id=rival.id
        )

    intruder = app.test_client()
    intruder.post("/auth/sign-in", data={"email": "rival@example.com", "password": PASSWORD})
    assert intruder.get(f"/projects/{project_id}/production").status_code == 404
    assert (
        intruder.post(
            f"/projects/{project_id}/production/commit",
            data={"description": "x", "crew": "y"},
        ).status_code
        == 404
    )


def test_a_viewer_cannot_commit(app, project_id) -> None:  # type: ignore[no-untyped-def]
    """Production control is a write, and the role check is the same one that
    guards every other write.
    """
    from massingplan.models.identity import Role

    with database.session_scope() as session:
        accounts.register(
            session,
            email="viewer@example.com",
            password=PASSWORD,
            organization_id=repo.DEFAULT_ORG_ID,
            role=Role.VIEWER,
        )

    viewer = app.test_client()
    viewer.post("/auth/sign-in", data={"email": "viewer@example.com", "password": PASSWORD})
    assert viewer.get(f"/projects/{project_id}/production").status_code == 200
    assert (
        viewer.post(
            f"/projects/{project_id}/production/commit",
            data={"description": "x", "crew": "y"},
        ).status_code
        == 403
    )
