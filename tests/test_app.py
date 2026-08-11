"""The web layer: routes, headers, templates, and the JSON API.

The route walk and the template scan are borrowed from the source repo's best
idea -- they enumerate the app rather than a hand-written list, so a page added
without a test still gets exercised, and a template referencing an endpoint that
does not exist fails here instead of in production.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from massingplan.app import create_app
from massingplan.config import Settings

TEMPLATES = Path(__file__).resolve().parent.parent / "massingplan" / "templates"


@pytest.fixture
def app(tmp_path):  # type: ignore[no-untyped-def]
    from massingplan import database
    from massingplan.services import repository as repo

    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key-not-a-secret",
            database_url=f"sqlite:///{tmp_path / 'app.db'}",
        )
    )
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    database.create_all()
    with database.session_scope() as session:
        repo.ensure_default_organization(session)
    return application


@pytest.fixture
def api_key(app):  # type: ignore[no-untyped-def]
    """A key for the JSON API, which refuses anonymous calls."""
    from massingplan import database
    from massingplan.services import accounts
    from massingplan.services import repository as repo

    with database.session_scope() as session:
        plaintext, _record = accounts.issue_api_key(
            session, organization_id=repo.DEFAULT_ORG_ID, name="tests"
        )
    return plaintext


@pytest.fixture
def client(app, api_key):  # type: ignore[no-untyped-def]
    """A client that presents the key on every request.

    The API is closed by default now; a client that does not authenticate
    exercises the 401 path and nothing else.
    """
    handle = app.test_client()
    handle.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {api_key}"
    return handle


@pytest.fixture
def signed_in(app):  # type: ignore[no-untyped-def]
    """A browser session for the page routes."""
    from massingplan import database
    from massingplan.services import accounts
    from massingplan.services import repository as repo

    with database.session_scope() as session:
        accounts.register(
            session,
            email="pages@example.com",
            password="a-long-enough-passphrase",
            organization_id=repo.DEFAULT_ORG_ID,
        )
    handle = app.test_client()
    handle.post(
        "/auth/sign-in",
        data={"email": "pages@example.com", "password": "a-long-enough-passphrase"},
    )
    return handle


# -- the route walk --------------------------------------------------------


def test_every_get_route_answers(app, signed_in) -> None:  # type: ignore[no-untyped-def]
    """Walk `url_map` rather than a hand-written list.

    In the source repo this found nine unwritten templates, two broken helper
    chains and a health check that had returned 503 for its entire life.
    """
    walked = 0
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.arguments:
            continue
        response = signed_in.get(rule.rule)
        assert response.status_code < 500, f"{rule.rule} returned {response.status_code}"
        walked += 1
    assert walked >= 6, f"the walk only covered {walked} routes"


def test_no_route_is_skipped_silently(app) -> None:
    """Guard the guard: adding a URL parameter must not quietly drop a route
    from the walk above without anyone noticing.
    """
    parameterised = {
        r.endpoint
        for r in app.url_map.iter_rules()
        # Flask's own static handler, not ours.
        if r.arguments and r.endpoint != "static"
    }
    # An allow-list rather than an empty assertion, because parameterised routes
    # are legitimate -- what is not legitimate is one nobody tests. Every entry
    # here is covered by name: the project pages and tenant isolation in
    # test_auth.py, key revocation and org switching likewise, webhook
    # removal in test_webhooks.py, and the location model in
    # test_location_persistence.py.
    covered_by_name = {
        "main.project_detail",
        "main.set_baseline",
        "main.delete_project",
        "main.export_xer",
        "main.revoke_key",
        "main.delete_webhook",
        "main.project_linear",
        "main.set_locations",
        "main.add_trade",
        "main.delete_trade",
        "auth.switch",
    }
    assert parameterised <= covered_by_name, (
        "these routes take parameters, are skipped by the route walk, and have "
        f"no named test: {sorted(parameterised - covered_by_name)}"
    )


URL_FOR = re.compile(r"""url_for\(\s*['"]([^'"]+)['"]""")


def test_every_url_for_in_a_template_resolves(app) -> None:  # type: ignore[no-untyped-def]
    """A `url_for` naming an endpoint that does not exist raises BuildError and
    takes the whole page down. Cheaper to find here.
    """
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    for template in TEMPLATES.rglob("*.html"):
        for endpoint in URL_FOR.findall(template.read_text(encoding="utf-8")):
            assert endpoint in endpoints, f"{template.name} references unknown endpoint {endpoint}"


def test_every_url_for_in_python_resolves(app) -> None:  # type: ignore[no-untyped-def]
    """The same check over the views themselves.

    The template version of this missed a `url_for("main.projects")` in a
    redirect -- the endpoint is `main.projects_list` -- and the only symptom was
    a 500 *after* the sign-in had already succeeded, so the user was logged in
    and staring at an error page.
    """
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    root = TEMPLATES.parent
    for module in root.rglob("*.py"):
        for endpoint in URL_FOR.findall(module.read_text(encoding="utf-8")):
            assert endpoint in endpoints, (
                f"{module.relative_to(root)} references unknown endpoint {endpoint}"
            )


# -- the CSP promise -------------------------------------------------------


def test_no_template_loads_anything_from_another_host(app) -> None:
    """The Gantt is self-hosted precisely so `default-src 'self'` can stay
    strict. One CDN script tag silently undoes that, and the page keeps working,
    so nothing else would catch it.
    """
    external = re.compile(r"""(src|href)\s*=\s*["'](https?:)?//""")
    for template in TEMPLATES.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert not external.search(text), f"{template.name} loads from an external host"


def test_the_security_headers_are_set(signed_in) -> None:  # type: ignore[no-untyped-def]
    response = signed_in.get("/", follow_redirects=True)
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


# -- health ----------------------------------------------------------------


def test_healthz_is_liveness_and_readyz_actually_computes(client) -> None:  # type: ignore[no-untyped-def]
    """`readyz` schedules a one-activity network. Answering "ok" without
    checking would make the probe a decoration -- and a probe that is a
    decoration is how a container reports itself healthy for its entire life.
    """
    assert client.get("/healthz").get_json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.get_json()["status"] == "ready"


# -- the JSON API ----------------------------------------------------------


def test_schedule_endpoint_returns_dates(client) -> None:  # type: ignore[no-untyped-def]
    body = {
        "data_date": "2026-06-01",
        "activities": [
            {"id": "A", "name": "Excavate", "duration_days": 5},
            {"id": "B", "name": "Foundations", "duration_days": 10, "predecessors": ["A"]},
        ],
    }
    result = client.post("/api/massingplan/v1/schedule", json=body).get_json()
    rows = {r["activity_id"]: r for r in result["activities"]}
    assert rows["A"]["finish"] == "2026-06-05"
    assert rows["B"]["start"] == "2026-06-08"
    assert result["project_finish"] == "2026-06-19"


def test_relationship_types_and_lags_survive_the_api(client) -> None:  # type: ignore[no-untyped-def]
    body = {
        "data_date": "2026-06-01",
        "activities": [
            {"id": "A", "duration_days": 10},
            {
                "id": "B",
                "duration_days": 5,
                "predecessors": [{"id": "A", "type": "SS", "lag_days": 2}],
            },
        ],
    }
    rows = {
        r["activity_id"]: r
        for r in client.post("/api/massingplan/v1/schedule", json=body).get_json()["activities"]
    }
    assert rows["B"]["start"] == "2026-06-03"


def test_a_response_can_be_fed_straight_back_in(client) -> None:  # type: ignore[no-untyped-def]
    """The output's `predecessors` is the same shape as the input's.

    It was a list of bare id strings for one commit, and `_tasks_and_links`
    reads a bare string as Finish-Start with zero lag -- so resubmitting a
    response silently flattened every SS, FF and SF tie and dropped every lag.
    The schedule came back different with nothing reporting a problem, which is
    the worst way for an API to be wrong.
    """
    body = {
        "data_date": "2026-06-01",
        "activities": [
            {"id": "A", "duration_days": 10},
            {
                "id": "B",
                "duration_days": 5,
                "predecessors": [{"id": "A", "type": "SS", "lag_days": 2}],
            },
        ],
    }
    first = client.post("/api/massingplan/v1/schedule", json=body).get_json()

    # Rename the row key to the input key and resubmit, changing nothing else.
    replayed = {
        "data_date": "2026-06-01",
        "activities": [
            {
                "id": row["activity_id"],
                "duration_days": row["duration_days"],
                "predecessors": row["predecessors"],
            }
            for row in first["activities"]
        ],
    }
    second = client.post("/api/massingplan/v1/schedule", json=replayed).get_json()

    assert second["project_finish"] == first["project_finish"]
    assert {r["activity_id"]: r["start"] for r in second["activities"]} == {
        r["activity_id"]: r["start"] for r in first["activities"]
    }
    # And the tie really did survive as SS+2 rather than being read as FS.
    replayed_b = next(r for r in second["activities"] if r["activity_id"] == "B")
    assert replayed_b["start"] == "2026-06-03"


def test_analyse_returns_a_grade_with_skipped_checks_excluded(client) -> None:  # type: ignore[no-untyped-def]
    body = {
        "data_date": "2026-06-01",
        "activities": [
            {"id": f"A{i}", "duration_days": 4, "predecessors": ([f"A{i - 1}"] if i else [])}
            for i in range(5)
        ],
    }
    health = client.post("/api/massingplan/v1/analyse", json=body).get_json()["health"]
    assert health["grade"] in "ABCDF"
    assert health["assessed"] + health["skipped"] == 14
    assert health["skipped"] == 4


def test_risk_is_seeded_so_two_calls_agree(client) -> None:  # type: ignore[no-untyped-def]
    body = {
        "data_date": "2026-06-01",
        "iterations": 100,
        "activities": [
            {"id": "A", "duration_days": 10},
            {"id": "B", "duration_days": 5, "predecessors": ["A"]},
        ],
    }
    first = client.post("/api/massingplan/v1/risk", json=body).get_json()
    second = client.post("/api/massingplan/v1/risk", json=body).get_json()
    assert first == second
    assert first["p80"] >= first["p50"]


def test_a_cycle_is_a_422_naming_the_loop(client) -> None:  # type: ignore[no-untyped-def]
    body = {
        "activities": [
            {"id": "A", "duration_days": 1, "predecessors": ["B"]},
            {"id": "B", "duration_days": 1, "predecessors": ["A"]},
        ]
    }
    response = client.post("/api/massingplan/v1/schedule", json=body)
    assert response.status_code == 422
    error = response.get_json()["error"]
    assert error["code"] == "validation_failed"
    assert set(error["detail"]["cycle"]) == {"A", "B"}


def test_a_non_json_body_is_refused_with_the_reason(client) -> None:  # type: ignore[no-untyped-def]
    response = client.post(
        "/api/massingplan/v1/schedule",
        data="id=A",
        content_type="application/x-www-form-urlencoded",
    )
    assert response.status_code == 400
    assert "application/json" in response.get_json()["error"]["message"]


def test_an_unknown_relationship_type_lists_the_valid_ones(client) -> None:  # type: ignore[no-untyped-def]
    body = {
        "activities": [
            {"id": "A", "duration_days": 1},
            {"id": "B", "duration_days": 1, "predecessors": [{"id": "A", "type": "XX"}]},
        ]
    }
    response = client.post("/api/massingplan/v1/schedule", json=body)
    assert response.status_code == 422
    assert "FS" in response.get_json()["error"]["detail"]


def test_capabilities_names_what_is_not_supported(client) -> None:  # type: ignore[no-untyped-def]
    """A client that has to POST an .mpp to discover it is unsupported has
    learned it the expensive way.
    """
    caps = client.get("/api/massingplan/v1/capabilities").get_json()
    assert caps["formats"]["read"] == ["xer", "mspdi"]
    assert "proprietary" in caps["formats"]["unsupported"]["mpp"]
    assert "dcma_14_point" in caps["features"]


# -- import ----------------------------------------------------------------


XER = (
    "ERMHDR\t19.12\t2026-08-08\tProject\tadmin\n"
    "%T\tPROJECT\n%F\tproj_id\tproj_short_name\tlast_recalc_date\tclndr_id\n"
    "%R\t1\tDEMO\t2026-06-01 00:00\tC1\n"
    "%T\tCALENDAR\n%F\tclndr_id\tclndr_name\tday_hr_cnt\tdefault_flag\tclndr_data\n"
    "%R\tC1\tStandard\t8\tY\t(0||DaysOfWeek()((1())(2()(0||1(s|08:00|f|16:00)))"
    "(3()(0||1(s|08:00|f|16:00)))(4()(0||1(s|08:00|f|16:00)))(5()(0||1(s|08:00|f|16:00)))"
    "(6()(0||1(s|08:00|f|16:00)))(7())))\n"
    "%T\tTASK\n%F\ttask_id\tproj_id\ttask_code\ttask_name\ttask_type\tclndr_id\ttarget_drtn_hr_cnt\n"
    "%R\tT1\t1\tA1010\tExcavate\tTT_Task\tC1\t40\n"
    "%R\tT2\t1\tA1020\tFoundations\tTT_Task\tC1\t80\n"
    "%T\tTASKPRED\n%F\ttask_pred_id\ttask_id\tpred_task_id\tpred_type\tlag_hr_cnt\n"
    "%R\t1\tT2\tT1\tPR_FS\t0\n"
    "%E\n"
)


def test_importing_an_xer_through_the_api(client) -> None:  # type: ignore[no-untyped-def]
    data = {"file": (io.BytesIO(XER.encode()), "tower.xer")}
    result = client.post(
        "/api/massingplan/v1/import", data=data, content_type="multipart/form-data"
    ).get_json()
    assert result["has_logic"] is True
    assert result["source"]["relationships"] == 1
    assert len(result["activities"]) == 2


def test_importing_through_the_web_page_renders_the_workspace(signed_in) -> None:  # type: ignore[no-untyped-def]
    data = {"file": (io.BytesIO(XER.encode()), "tower.xer")}
    response = signed_in.post(
        "/upload", data=data, content_type="multipart/form-data", follow_redirects=True
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "A1010" in body


def test_an_unreadable_upload_is_refused_by_name(client) -> None:  # type: ignore[no-untyped-def]
    data = {"file": (io.BytesIO(b"this is not a schedule"), "notes.txt")}
    response = client.post(
        "/api/massingplan/v1/import", data=data, content_type="multipart/form-data"
    )
    assert response.status_code == 415
    assert "could not be read" in response.get_json()["error"]["message"]


def test_uploading_nothing_says_so(client) -> None:  # type: ignore[no-untyped-def]
    response = client.post(
        "/api/massingplan/v1/import", data={}, content_type="multipart/form-data"
    )
    assert response.status_code == 400


# -- the demo page ---------------------------------------------------------


def test_the_demo_page_shows_a_grade_and_a_gantt(client) -> None:  # type: ignore[no-untyped-def]
    """The gap named first in the source repo's own README: it computes the
    grade, the offenders and the P80 and surfaces none of them.
    """
    body = client.get("/demo").get_data(as_text=True)
    assert "DCMA" in body
    assert 'id="gantt"' in body
    assert "Topping out" in body or "A2000" in body
    # Skipped checks must be visible as skipped, not folded into a pass.
    assert "skipped" in body


def test_the_gantt_receives_json_the_browser_can_parse(client) -> None:  # type: ignore[no-untyped-def]
    from html import unescape

    body = client.get("/demo").get_data(as_text=True)
    match = re.search(r'data-activities="([^"]*)"', body)
    assert match, "the Gantt host carries no activity data"
    rows = json.loads(unescape(match.group(1)))
    assert rows and "start" in rows[0] and "is_critical" in rows[0]


def test_production_refuses_to_boot_without_a_secret_key() -> None:
    """Four workers with four generated keys invalidate each other's sessions,
    and the symptom -- "users get logged out at random" -- is a long way from
    the cause.
    """
    with pytest.raises(RuntimeError, match="MASSINGPLAN_SECRET_KEY"):
        create_app(Settings(env="production", secret_key=""))
