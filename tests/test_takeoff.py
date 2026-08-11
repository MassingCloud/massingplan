"""The take-off: quantities and a production rate instead of a flat duration.

A rate with no quantities is a number with no work attached to it, so until the
form could take a take-off the production-rate path existed in the engine, in
the model and in the API, and was unreachable from the page. What follows
covers the parser on its own -- it is the piece a planner types into, so its
failure modes are the ones that matter -- and then the route around it.

The distinction the parser exists to preserve: a quantity typed against a
location that is not in the breakdown is a take-off the planner believes is in
the model and is not. Dropping it silently produces a schedule that is short by
exactly the work nobody will check for.
"""

from __future__ import annotations

import io
from datetime import date

import pytest

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings
from massingplan.models import Project
from massingplan.services import accounts, projects
from massingplan.services import repository as repo

PASSWORD = "a-long-enough-passphrase"

XER = (
    "%T\tPROJECT\n%F\tproj_id\tproj_short_name\n%R\t1\tTOWER\n"
    "%T\tTASK\n%F\ttask_id\tproj_id\ttask_code\ttask_name\ttarget_drtn_hr_cnt\n"
    "%R\t10\t1\tA1000\tExcavate\t40\n%E\n"
)


@pytest.fixture
def app(tmp_path):  # type: ignore[no-untyped-def]
    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / 'takeoff.db'}",
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
        data={"file": (io.BytesIO(XER.encode()), "job.xer")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302, response.get_data(as_text=True)[:300]
    return response.headers["Location"].rstrip("/").rsplit("/", 1)[-1]


def _durations(project_id: str) -> dict[str, int]:
    """Working days per location, computed from the stored model."""
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        linear = projects.linear_schedule(project, start=date(2026, 3, 2))
        assert linear is not None
        return {row["location_id"]: row["duration_days"] for row in linear["segments"]}


# -- the parser ------------------------------------------------------------


def test_a_bare_number_is_the_quantity_for_every_location() -> None:
    """The common case: the same floor plate repeated up the building."""
    quantities, problems = projects.parse_quantities("380", ["L1", "L2", "L3"])
    assert problems == []
    assert quantities == {"L1": 380.0, "L2": 380.0, "L3": 380.0}


def test_a_later_line_overrides_the_default() -> None:
    """The order a planner states it in: "380 a floor, except the third"."""
    quantities, problems = projects.parse_quantities("380\nL3 | 200", ["L1", "L2", "L3"])
    assert problems == []
    assert quantities == {"L1": 380.0, "L2": 380.0, "L3": 200.0}


def test_a_default_after_an_override_wins_because_it_is_later() -> None:
    """Last line wins, both directions. Stated once so the rule cannot quietly
    become "an override always beats a default", which reads the same on the
    happy path and differently on this one.
    """
    quantities, _ = projects.parse_quantities("L3 | 200\n380", ["L1", "L2", "L3"])
    assert quantities["L3"] == 380.0


def test_a_quantity_against_a_location_that_does_not_exist_is_reported() -> None:
    quantities, problems = projects.parse_quantities("Level 8 | 200", ["L1", "L2"])
    assert quantities == {}
    assert len(problems) == 1
    assert "Level 8" in problems[0]
    assert "line 1" in problems[0]


def test_a_non_number_is_reported_with_the_line_it_was_on() -> None:
    """Forty floors in the box, and "which one" is the only useful part."""
    quantities, problems = projects.parse_quantities("380\nL2 | lots", ["L1", "L2"])
    assert len(problems) == 1
    assert "line 2" in problems[0]
    assert quantities == {"L1": 380.0, "L2": 380.0}


def test_a_negative_quantity_is_not_work() -> None:
    quantities, problems = projects.parse_quantities("L1 | -5", ["L1"])
    assert quantities == {}
    assert problems and "negative" in problems[0]


def test_blank_lines_and_padding_are_not_errors() -> None:
    quantities, problems = projects.parse_quantities("\n  L1 | 200  \n\n", ["L1"])
    assert problems == []
    assert quantities == {"L1": 200.0}


def test_zero_is_a_quantity_and_not_a_missing_one() -> None:
    """A floor with nothing in it for this trade is a real answer, and it has to
    survive the parser rather than being read as "not stated".
    """
    quantities, problems = projects.parse_quantities("380\nL2 | 0", ["L1", "L2"])
    assert problems == []
    assert quantities["L2"] == 0.0


@pytest.mark.parametrize("word", ["nan", "inf", "-inf", "Infinity", "NaN"])
def test_the_two_words_float_accepts_that_are_not_quantities(word: str) -> None:
    """`float()` parses these, and then they survive every ordered comparison --
    `nan < 0` is False -- and reach `math.ceil` in the engine, which raises. A
    500 from something typed into a box is a worse answer than a named line.
    """
    quantities, problems = projects.parse_quantities(f"L1 | {word}", ["L1"])
    assert quantities == {}
    assert len(problems) == 1


# -- the route -------------------------------------------------------------


def test_a_take_off_becomes_a_duration_per_location(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """380 m2 at 95 m2/day is four days; the floor holding 190 is two.

    This is the whole point of the feature. Before it, every location of a
    trade ran for the same flat duration whatever was in it.
    """
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1\nL2\nL3"})
    response = client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "rate": "95", "quantities": "380\nL3 | 190"},
    )
    assert response.status_code == 302, response.get_data(as_text=True)[:400]
    assert _durations(project_id) == {"L1": 4, "L2": 4, "L3": 2}


def test_a_quantity_that_does_not_divide_evenly_rounds_up(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """Two-thirds of a crew-day is a day on site."""
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1"})
    client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "rate": "95", "quantities": "200"},
    )
    assert _durations(project_id) == {"L1": 3}


def test_a_location_without_a_quantity_keeps_the_flat_duration(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """A partial take-off is normal -- one floor measured, the rest estimated --
    and the unmeasured floors must not collapse to zero days.
    """
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1\nL2"})
    client.post(
        f"/projects/{project_id}/linear/trades",
        data={
            "key": "Drywall",
            "rate": "95",
            "duration_days": "6",
            "quantities": "L1 | 190",
        },
    )
    assert _durations(project_id) == {"L1": 2, "L2": 6}


def test_a_take_off_for_a_location_that_is_not_there_is_refused(client, project_id) -> None:  # type: ignore[no-untyped-def]
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1\nL2"})
    response = client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "rate": "95", "quantities": "380\nLevel 8 | 200"},
    )
    assert response.status_code == 400
    assert "Level 8" in response.get_data(as_text=True)

    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert project.linear_activities == [], "a rejected form must store nothing"


def test_every_problem_is_named_not_just_the_first(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """Reporting one error at a time turns a forty-line take-off into forty
    round trips.
    """
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1\nL2"})
    body = client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "rate": "95", "quantities": "Level 8 | 90\nL2 | plenty"},
    ).get_data(as_text=True)
    assert "Level 8" in body
    assert "plenty" in body


def test_a_rejected_form_hands_back_what_was_typed(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """Naming line 37 and then clearing the box is not materially better than
    not naming it.
    """
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1\nL2"})
    body = client.post(
        f"/projects/{project_id}/linear/trades",
        data={
            "key": "Drywall",
            "name": "Drywall and taping",
            "rate": "95",
            "buffer_days": "3",
            "crews": "2",
            "quantities": "380\nLevel 8 | 200",
        },
    ).get_data(as_text=True)
    assert 'value="Drywall"' in body
    assert 'value="Drywall and taping"' in body
    assert 'value="95"' in body
    assert 'value="3"' in body
    assert "Level 8 | 200" in body


def test_the_form_starts_empty_on_a_plain_visit(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """The other half of handing the form back: a GET must not inherit it."""
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1\nL2"})
    client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "rate": "95", "quantities": "L9 | 200"},
    )
    body = client.get(f"/projects/{project_id}/linear").get_data(as_text=True)
    assert 'id="trade-key" name="key" placeholder="Drywall" required\n      value=""' in body
    assert "L9 | 200" not in body
    assert '&#10;Level 8 | 200"></textarea>' in body, "the box is empty but for its placeholder"


def test_quantities_without_a_rate_are_refused_rather_than_ignored(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """Stored quantities with no rate compute nothing, so the trade would run at
    its flat duration with a take-off sitting next to it looking applied.
    """
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1\nL2"})
    response = client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "duration_days": "3", "quantities": "380"},
    )
    assert response.status_code == 400
    assert "production rate" in response.get_data(as_text=True)


def test_a_rate_that_is_not_a_number_is_refused_with_a_reason(client, project_id) -> None:  # type: ignore[no-untyped-def]
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1"})
    response = client.post(
        f"/projects/{project_id}/linear/trades", data={"key": "Drywall", "rate": "fast"}
    )
    assert response.status_code == 400
    assert "production rate" in response.get_data(as_text=True)


@pytest.mark.parametrize("rate", ["0", "-5", "nan", "inf"])
def test_a_rate_that_is_not_a_positive_finite_number_is_refused(client, project_id, rate) -> None:  # type: ignore[no-untyped-def]
    """400 for all four, not a 500 for two of them. `nan` passes `<= 0` and
    divides into a quantity that `math.ceil` then refuses to convert.
    """
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1"})
    response = client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "rate": rate, "quantities": "380"},
    )
    assert response.status_code == 400, f"{rate!r} got {response.status_code}"


def test_a_rejected_breakdown_is_shown_back_as_typed(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """Forty floors refused for a duplicate on line 19 is forty floors to
    re-enter if the page answers with the stored list instead.
    """
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1\nL2"})
    body = client.post(
        f"/projects/{project_id}/linear/locations",
        data={"locations": "Ground\nLevel 1\nLevel 2\nLevel 1"},
    ).get_data(as_text=True)
    assert "share a key" in body
    assert "Ground\nLevel 1\nLevel 2\nLevel 1" in body
    assert ">L1\n" not in body, "the stored list must not replace what was typed"


def test_an_empty_take_off_box_leaves_the_stored_one_alone(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """Correcting a buffer must not wipe a take-off by omission. Clearing the
    *rate* is how a planner goes back to a flat duration.
    """
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1\nL2"})
    client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "rate": "95", "quantities": "380"},
    )
    client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "rate": "95", "buffer_days": "2", "quantities": ""},
    )
    with database.session_scope() as session:
        project = session.get(Project, project_id)
        assert project is not None
        trade = project.linear_activities[0]
        assert trade.buffer_days == 2
        assert {q.quantity for q in trade.quantities} == {380.0}


def test_clearing_the_rate_returns_the_trade_to_its_flat_duration(client, project_id) -> None:  # type: ignore[no-untyped-def]
    """The documented way out, asserted -- otherwise the page tells the planner
    to do something that does not work.
    """
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1"})
    client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "rate": "95", "quantities": "380"},
    )
    assert _durations(project_id) == {"L1": 4}
    client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "rate": "", "duration_days": "7"},
    )
    assert _durations(project_id) == {"L1": 7}


def test_the_page_shows_what_is_stored_against_each_location(client, project_id) -> None:  # type: ignore[no-untyped-def]
    client.post(f"/projects/{project_id}/linear/locations", data={"locations": "L1\nL2"})
    client.post(
        f"/projects/{project_id}/linear/trades",
        data={"key": "Drywall", "rate": "95", "quantities": "380\nL2 | 190"},
    )
    body = client.get(f"/projects/{project_id}/linear").get_data(as_text=True)
    assert "95.0/day" in body
    assert "L2: 190.0" in body
    assert 'name="quantities"' in body, "the form has to offer the box it documents"
