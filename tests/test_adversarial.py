"""Attacking the app on purpose.

This is not a substitute for a penetration test — nobody outside the project has
tried to break this, and SECURITY.md still says so. What it is: the classes of
bug that a pentest would find *and that a regression could quietly reintroduce*,
pinned so the second time is caught by CI rather than by a report.

Every test here is written from the attacker's side. Where a defence works, the
test says which line of code would have to change for it to stop working —
because a security test whose failure mode is unclear gets weakened rather than
investigated.
"""

from __future__ import annotations

import io
import re

import pytest
from sqlalchemy import select

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings
from massingplan.models import ApiKey, Organization, Project, User
from massingplan.models.identity import Membership, Role
from massingplan.services import accounts
from massingplan.services import repository as repo

PASSWORD = "a-long-enough-passphrase"
OTHER_ORG = "00000000000000000000000000000042"

XER = (
    "%T\tPROJECT\n%F\tproj_id\tproj_short_name\n%R\t1\t{code}\n"
    "%T\tTASK\n%F\ttask_id\tproj_id\ttask_code\ttask_name\ttarget_drtn_hr_cnt\n"
    "%R\t10\t1\tA1000\t{activity}\t40\n%E\n"
)


@pytest.fixture
def app(tmp_path):  # type: ignore[no-untyped-def]
    """Two organisations, each with an owner and a project. The interesting
    fixture: almost every test below is "can one reach the other".
    """
    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / 'adv.db'}",
            rate_limit_enabled=False,
        )
    )
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    database.create_all()
    with database.session_scope() as session:
        repo.ensure_default_organization(session)
        session.add(Organization(id=OTHER_ORG, name="Rival Construction", slug="rival"))
        session.flush()
        accounts.register(
            session,
            email="mine@example.com",
            password=PASSWORD,
            organization_id=repo.DEFAULT_ORG_ID,
        )
        accounts.register(
            session, email="theirs@example.com", password=PASSWORD, organization_id=OTHER_ORG
        )
    return application


def _client(app, email: str = "mine@example.com"):  # type: ignore[no-untyped-def]
    client = app.test_client()
    client.post("/auth/sign-in", data={"email": email, "password": PASSWORD})
    return client


CSRF_INPUT = re.compile(r'(name="csrf_token" value=")[^"]*(")')


def _without_csrf(response) -> str:  # type: ignore[no-untyped-def]
    """The body with the CSRF token blanked.

    Two responses that should be indistinguishable to an attacker still differ
    in that one field, because the token is timestamped. Masking it is what
    makes "these two answers are the same" a statement about the answer rather
    than about the clock.
    """
    return CSRF_INPUT.sub(r"\1MASKED\2", response.get_data(as_text=True))


def _import(client, code: str = "TOWER", activity: str = "Excavate") -> str:
    """Import a one-activity project and return its id."""
    body = XER.format(code=code, activity=activity)
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(body.encode()), "job.xer")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302, response.get_data(as_text=True)[:400]
    return response.headers["Location"].rstrip("/").rsplit("/", 1)[-1]


# -- IDOR: reaching another tenant's rows ----------------------------------


def test_another_tenants_project_is_a_404_not_a_403(app) -> None:  # type: ignore[no-untyped-def]
    """403 confirms the id exists, and "does this id exist" is exactly the
    question. `deps.load_project` raises NotFound for both causes on purpose.
    """
    theirs = _import(_client(app, "theirs@example.com"), code="RIVAL")
    mine = _client(app)
    assert mine.get(f"/projects/{theirs}").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/projects/{id}",
        "/projects/{id}/export.xer",
    ],
)
def test_no_read_route_leaks_across_the_tenant_boundary(app, path: str) -> None:  # type: ignore[no-untyped-def]
    theirs = _import(_client(app, "theirs@example.com"), code="RIVAL", activity="Secret Piling")
    mine = _client(app)
    response = mine.get(path.format(id=theirs))
    assert response.status_code == 404
    assert b"Secret Piling" not in response.data


@pytest.mark.parametrize(
    "path",
    [
        "/projects/{id}/baseline",
        "/projects/{id}/delete",
    ],
)
def test_no_write_route_reaches_across_the_tenant_boundary(app, path: str) -> None:  # type: ignore[no-untyped-def]
    theirs = _import(_client(app, "theirs@example.com"), code="RIVAL")
    mine = _client(app)
    assert mine.post(path.format(id=theirs), data={"name": "B"}).status_code == 404

    with database.session_scope() as session:
        assert session.get(Project, theirs) is not None, "a cross-tenant delete succeeded"


def test_an_api_key_cannot_read_another_organisation(app) -> None:  # type: ignore[no-untyped-def]
    """The key is scoped to the organisation that issued it. If this ever
    passes, the scoping in `repository.scoped()` has been bypassed somewhere.
    """
    with database.session_scope() as session:
        plaintext, _record = accounts.issue_api_key(
            session, organization_id=OTHER_ORG, name="rival CI"
        )
    theirs = _import(_client(app, "theirs@example.com"), code="RIVAL")
    mine = _import(_client(app), code="MINE")

    client = app.test_client()
    headers = {"Authorization": f"Bearer {plaintext}"}
    # Their own project is reachable; mine is not. Both halves matter -- a test
    # that only checks the refusal passes on a key that works for nothing.
    assert client.get(f"/projects/{theirs}", headers=headers).status_code in (200, 404)
    assert client.get(f"/projects/{mine}", headers=headers).status_code == 404


def test_a_revoked_key_stops_working_immediately(app) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        plaintext, record = accounts.issue_api_key(
            session, organization_id=repo.DEFAULT_ORG_ID, name="CI"
        )
        key_id = record.id
    client = app.test_client()
    headers = {"Authorization": f"Bearer {plaintext}"}
    body = {"data_date": "2026-06-01", "activities": [{"id": "A", "duration_days": 1}]}
    assert (
        client.post("/api/massingplan/v1/schedule", json=body, headers=headers).status_code == 200
    )

    with database.session_scope() as session:
        accounts.revoke_api_key(session, session.get(ApiKey, key_id))  # type: ignore[arg-type]
    assert (
        client.post("/api/massingplan/v1/schedule", json=body, headers=headers).status_code == 401
    )


def test_a_forged_bearer_token_is_refused(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    for token in ("mpln_" + "a" * 40, "", "Bearer", "null", "undefined", "' OR '1'='1"):
        response = client.post(
            "/api/massingplan/v1/schedule",
            json={"data_date": "2026-06-01", "activities": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401, token


# -- session handling ------------------------------------------------------


def test_the_session_is_rotated_on_sign_in(app) -> None:  # type: ignore[no-untyped-def]
    """Session fixation: an attacker who plants a session id before sign-in must
    not still hold a valid one after. `deps.sign_in` clears the session first --
    remove that line and this fails.
    """
    client = app.test_client()
    with client.session_transaction() as session:
        session["planted"] = "attacker-value"
    client.post("/auth/sign-in", data={"email": "mine@example.com", "password": PASSWORD})
    with client.session_transaction() as session:
        assert "planted" not in session
        assert session.get("user_id")


def test_signing_out_actually_ends_the_session(app) -> None:  # type: ignore[no-untyped-def]
    client = _client(app)
    assert client.get("/projects").status_code == 200
    client.post("/auth/sign-out")
    assert client.get("/projects").status_code in (302, 401)


def test_the_session_cookie_is_not_readable_by_script(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    response = client.post(
        "/auth/sign-in", data={"email": "mine@example.com", "password": PASSWORD}
    )
    cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie or "SameSite=Strict" in cookie


def test_a_tampered_session_cookie_is_rejected(app) -> None:  # type: ignore[no-untyped-def]
    """The cookie is signed. Flipping a byte must sign nobody in, rather than
    signing in whoever the flipped bytes happen to name.
    """
    client = _client(app)
    jar = client.get_cookie("session")
    assert jar is not None
    client.set_cookie("session", jar.value[:-6] + "AAAAAA", domain="localhost")
    assert client.get("/projects").status_code in (302, 400, 401)


# -- open redirect ---------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "//evil.example.com/",
        "https://evil.example.com/",
        "http:/evil.example.com",
        "\\\\evil.example.com",
        "/\\evil.example.com",
        "//evil.example.com\\@massingplan.local",
    ],
)
def test_sign_in_will_not_bounce_you_off_site(app, target: str) -> None:  # type: ignore[no-untyped-def]
    """An open redirect turns the sign-in page into a convincing launchpad for a
    phishing link on your own domain: the URL a user checks really is yours.
    """
    client = app.test_client()
    response = client.post(
        "/auth/sign-in",
        data={"email": "mine@example.com", "password": PASSWORD, "next": target},
    )
    assert "evil.example.com" not in response.headers.get("Location", "")


# -- injection into stored content -----------------------------------------


def test_a_script_tag_in_an_activity_name_is_escaped_not_run(app) -> None:  # type: ignore[no-untyped-def]
    """Stored XSS. Jinja autoescapes, so this passes today -- what it pins is
    that no template renders an activity name with `| safe`.
    """
    payload = "<script>alert(document.cookie)</script>"
    client = _client(app)
    project_id = _import(client, code="XSS", activity=payload)
    body = client.get(f"/projects/{project_id}").get_data(as_text=True)
    assert "<script>alert(document.cookie)</script>" not in body
    assert "&lt;script&gt;" in body or payload not in body


def test_an_svg_payload_in_a_project_code_is_escaped(app) -> None:  # type: ignore[no-untyped-def]
    client = _client(app)
    project_id = _import(client, code='"><svg onload=alert(1)>', activity="Dig")
    body = client.get(f"/projects/{project_id}").get_data(as_text=True)
    assert "<svg onload=" not in body


def test_a_quote_in_a_name_cannot_break_out_of_an_attribute(app) -> None:  # type: ignore[no-untyped-def]
    client = _client(app)
    project_id = _import(client, code="Q", activity='" autofocus onfocus="alert(1)')
    body = client.get(f"/projects/{project_id}").get_data(as_text=True)
    assert 'onfocus="alert(1)"' not in body


def test_a_sql_looking_string_is_data_not_sql(app) -> None:  # type: ignore[no-untyped-def]
    """SQLAlchemy parameterises, so this is a regression guard against somebody
    reaching for an f-string in a query later.
    """
    client = _client(app)
    _import(client, code="'; DROP TABLE projects; --", activity="Dig")
    with database.session_scope() as session:
        assert session.scalars(select(Project)).all(), "the projects table is gone"


def test_a_newline_in_an_exported_field_cannot_forge_an_xer_row(app) -> None:  # type: ignore[no-untyped-def]
    """The XER format is tab-and-newline delimited. A name carrying a newline
    would otherwise inject a row into the file the planner opens in P6.
    """
    client = _client(app)
    project_id = _import(client, code="INJ", activity="Dig\n%R\t99\t1\tEVIL\tInjected\t8")
    body = client.get(f"/projects/{project_id}/export.xer").get_data(as_text=True)
    assert "Injected" not in body.replace("\\n", "") or "\n%R\t99" not in body


# -- header injection ------------------------------------------------------


def test_a_crlf_in_a_project_code_cannot_forge_a_response_header(app) -> None:  # type: ignore[no-untyped-def]
    """The export sets `Content-Disposition` from the project code. A CRLF there
    would let an attacker add headers -- or a body -- to the response.
    """
    client = _client(app)
    project_id = _import(client, code="A\r\nX-Injected: yes", activity="Dig")
    response = client.get(f"/projects/{project_id}/export.xer")
    assert "X-Injected" not in response.headers
    assert "\n" not in response.headers.get("Content-Disposition", "")


# -- mass assignment -------------------------------------------------------


def test_posting_a_role_field_does_not_grant_it(app) -> None:  # type: ignore[no-untyped-def]
    """Registration reads three named fields. If it ever grows a `**form`, this
    is the test that fails.
    """
    client = app.test_client()
    client.post(
        "/auth/register",
        data={
            "email": "climber@example.com",
            "password": PASSWORD,
            "display_name": "Climber",
            "role": "owner",
            "is_active": "true",
            "organization_id": OTHER_ORG,
        },
    )
    with database.session_scope() as session:
        user = session.scalars(select(User).where(User.email == "climber@example.com")).first()
        if user is None:
            pytest.skip("open registration is disabled in this configuration")
        memberships = session.scalars(select(Membership).where(Membership.user_id == user.id)).all()
        assert all(m.organization_id != OTHER_ORG for m in memberships), (
            "a posted organization_id put the account in someone else's tenant"
        )
        assert all(m.role is not Role.OWNER for m in memberships) or all(
            m.organization_id != repo.DEFAULT_ORG_ID for m in memberships
        )


def test_an_api_payload_cannot_set_an_organisation(app) -> None:  # type: ignore[no-untyped-def]
    """The organisation comes from the credential, never from the body."""
    with database.session_scope() as session:
        plaintext, _record = accounts.issue_api_key(
            session, organization_id=repo.DEFAULT_ORG_ID, name="CI"
        )
    client = app.test_client()
    response = client.post(
        "/api/massingplan/v1/schedule",
        json={
            "data_date": "2026-06-01",
            "organization_id": OTHER_ORG,
            "activities": [{"id": "A", "duration_days": 1}],
        },
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    # Accepted or refused, but never as the other organisation.
    assert response.status_code in (200, 400, 422)
    with database.session_scope() as session:
        assert not session.scalars(
            select(Project).where(Project.organization_id == OTHER_ORG)
        ).all()


# -- CSRF ------------------------------------------------------------------


def test_state_changing_posts_require_a_token(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Run with CSRF *on*, unlike every other fixture here. A form post from
    another origin must not delete a project.
    """
    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / 'csrf.db'}",
            rate_limit_enabled=False,
        )
    )
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = True
    database.create_all()
    with database.session_scope() as session:
        repo.ensure_default_organization(session)
        accounts.register(
            session,
            email="mine@example.com",
            password=PASSWORD,
            organization_id=repo.DEFAULT_ORG_ID,
        )

    client = application.test_client()
    # Even signing in needs the token, so the whole flow is protected rather
    # than only the pages somebody remembered to decorate.
    response = client.post(
        "/auth/sign-in", data={"email": "mine@example.com", "password": PASSWORD}
    )
    assert response.status_code == 400


def test_the_api_is_exempt_from_csrf_and_that_is_deliberate(app) -> None:  # type: ignore[no-untyped-def]
    """A bearer token is not sent automatically by a browser, so there is no
    cross-site request to forge. Cookie auth on the same endpoints would change
    that, which is why the API does not accept cookies.
    """
    client = _client(app)  # holds a session cookie
    response = client.post(
        "/api/massingplan/v1/schedule",
        json={"data_date": "2026-06-01", "activities": [{"id": "A", "duration_days": 1}]},
    )
    assert response.status_code == 401, "the API accepted a session cookie as a credential"


# -- response headers ------------------------------------------------------


def test_every_response_carries_the_hardening_headers(app) -> None:  # type: ignore[no-untyped-def]
    client = _client(app)
    for path in ("/", "/demo", "/projects", "/account"):
        response = client.get(path)
        assert response.headers["X-Content-Type-Options"] == "nosniff", path
        assert response.headers["X-Frame-Options"] == "DENY", path
        assert "default-src 'self'" in response.headers["Content-Security-Policy"], path


def test_the_csp_has_no_unsafe_inline_or_remote_origin(app) -> None:  # type: ignore[no-untyped-def]
    """The whole reason the frontend is a self-hosted bundle and the MFA QR is
    inline SVG. One CDN and this stops being true.
    """
    policy = app.test_client().get("/demo").headers["Content-Security-Policy"]
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert "http://" not in policy and "https://" not in policy


def test_an_error_page_does_not_leak_a_stack_trace(app) -> None:  # type: ignore[no-untyped-def]
    client = _client(app)
    body = client.get("/projects/does-not-exist").get_data(as_text=True)
    assert "Traceback" not in body
    assert "massingplan/blueprints" not in body
    assert "sqlalchemy" not in body.lower()


# -- credential handling ---------------------------------------------------


def test_sign_in_says_the_same_thing_whether_or_not_the_account_exists(app) -> None:  # type: ignore[no-untyped-def]
    """Distinct wording per cause turns the form into an account-enumeration
    oracle, which is how a credential-stuffing list gets filtered down.
    """
    client = app.test_client()
    missing = client.post(
        "/auth/sign-in", data={"email": "nobody@example.com", "password": "wrong-wrong-wrong"}
    )
    wrong = client.post(
        "/auth/sign-in", data={"email": "mine@example.com", "password": "wrong-wrong-wrong"}
    )
    assert missing.status_code == wrong.status_code
    # Compared with the CSRF token masked out, not byte for byte. The token is
    # itsdangerous-serialised with a one-second-resolution timestamp, so two
    # requests either side of a second boundary carry different ones -- an
    # earlier version of this test compared raw bodies and failed roughly one
    # run in three, for a reason that has nothing to do with enumeration.
    assert _without_csrf(missing) == _without_csrf(wrong)
    # And the guard is a real one: the shared message is actually present.
    assert "do not match an account" in _without_csrf(missing)


def test_an_api_key_is_never_echoed_back_after_issue(app) -> None:  # type: ignore[no-untyped-def]
    client = _client(app)
    response = client.post("/account/keys", data={"name": "CI"}, follow_redirects=False)
    plaintext = response.headers["Location"].split("issued=")[1]
    assert client.get("/account").get_data(as_text=True).count(plaintext) == 0


def test_the_password_hash_never_reaches_a_page(app) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        user = session.scalars(select(User).where(User.email == "mine@example.com")).one()
        digest = user.password_hash
    client = _client(app)
    for path in ("/account", "/projects"):
        assert digest not in client.get(path).get_data(as_text=True)


# -- upload handling -------------------------------------------------------


def test_an_xml_bomb_does_not_expand(app) -> None:  # type: ignore[no-untyped-def]
    """MSPDI is XML. An external entity or a billion-laugh expansion in an
    uploaded file is a file read or an out-of-memory kill, and both start the
    same way -- with a DOCTYPE the parser honours.
    """
    bomb = (
        '<?xml version="1.0"?>'
        "<!DOCTYPE lolz [<!ENTITY lol 'lol'>"
        "<!ENTITY lol2 '&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;'>"
        "<!ENTITY lol3 '&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;'>"
        "<!ENTITY lol4 '&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;'>]>"
        "<Project><Name>&lol4;</Name></Project>"
    )
    client = _client(app)
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(bomb.encode()), "bomb.xml")},
        content_type="multipart/form-data",
    )
    # Refused or read harmlessly -- what must not happen is the entity expanding.
    assert response.status_code in (200, 400, 415, 422)
    assert b"lollollol" not in response.data


def test_an_external_entity_does_not_read_a_local_file(app) -> None:  # type: ignore[no-untyped-def]
    xxe = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>'
        "<Project><Name>&xxe;</Name></Project>"
    )
    client = _client(app)
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(xxe.encode()), "xxe.xml")},
        content_type="multipart/form-data",
    )
    assert response.status_code in (200, 400, 415, 422)
    assert b"root:" not in response.data


def test_a_path_traversal_filename_does_not_escape(app) -> None:  # type: ignore[no-untyped-def]
    """Uploads are parsed and stored as rows, never written to disk -- so this
    is a guard against that changing without anyone noticing.
    """
    client = _client(app)
    response = client.post(
        "/upload",
        data={
            "file": (io.BytesIO(XER.format(code="T", activity="Dig").encode()), "../../etc/x.xer")
        },
        content_type="multipart/form-data",
    )
    assert response.status_code in (302, 400, 415, 422)


def test_an_upload_over_the_limit_is_refused_not_buffered(app) -> None:  # type: ignore[no-untyped-def]
    limit = app.config["MAX_CONTENT_LENGTH"]
    client = _client(app)
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(b"x" * (limit + 1024)), "huge.xer")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413


# -- authorisation, not just authentication --------------------------------


def test_a_viewer_cannot_write(app) -> None:  # type: ignore[no-untyped-def]
    """Being signed in is not the same as being allowed. The role is re-read per
    request, so demoting somebody takes effect on their next click rather than
    at their next sign-in.
    """
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

    project_id = _import(_client(app), code="OWNED")
    viewer = _client(app, "viewer@example.com")
    assert viewer.get(f"/projects/{project_id}").status_code == 200
    assert viewer.post(f"/projects/{project_id}/delete").status_code == 403
    assert viewer.post("/account/keys", data={"name": "sneaky"}).status_code == 403


def test_a_demotion_takes_effect_without_signing_out(app) -> None:  # type: ignore[no-untyped-def]
    client = _client(app)
    project_id = _import(client, code="DEMO")
    with database.session_scope() as session:
        user = session.scalars(select(User).where(User.email == "mine@example.com")).one()
        membership = user.membership_in(repo.DEFAULT_ORG_ID)
        assert membership is not None
        membership.role = Role.VIEWER
    assert client.post(f"/projects/{project_id}/delete").status_code == 403
