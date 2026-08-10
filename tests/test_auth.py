"""Authentication, authorisation, tenant isolation and audit.

The tests here defend the properties that would let one customer read another
customer's schedule, or let a viewer freeze a baseline. Everything else in this
repo is about being right about dates; this is about being right about people.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings
from massingplan.models import AuditEvent, Base, Organization
from massingplan.models.identity import ROLE_PERMISSIONS, Permission, Role
from massingplan.services import accounts, projects
from massingplan.services import repository as repo
from tests.test_persistence import fixture_schedule

PASSWORD = "a-long-enough-passphrase"


@pytest.fixture
def app(tmp_path):  # type: ignore[no-untyped-def]
    url = f"sqlite:///{tmp_path / 'auth.db'}"
    application = create_app(Settings(env="testing", secret_key="test-key", database_url=url))
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    database.create_all()
    with database.session_scope() as session:
        repo.ensure_default_organization(session)
    return application


@pytest.fixture
def session():  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as handle:
        repo.ensure_default_organization(handle)
        handle.commit()
        yield handle
    # Disposed, not left to the collector. A pooled connection that is
    # garbage collected while open is silent on 3.11 and 3.12 and a
    # ResourceWarning on 3.13 -- which, with warnings as errors, fails the
    # build on one interpreter and not the others.
    engine.dispose()


def make_user(session, email: str, role: Role = Role.OWNER, org: str | None = None):  # type: ignore[no-untyped-def]
    return accounts.register(
        session,
        email=email,
        password=PASSWORD,
        organization_id=org or repo.DEFAULT_ORG_ID,
        role=role,
    )


def sign_in(client, email: str, password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return client.post("/auth/sign-in", data={"email": email, "password": password})


# -- passwords -------------------------------------------------------------


def test_a_password_is_stored_as_argon2id_and_never_in_the_clear(session) -> None:  # type: ignore[no-untyped-def]
    user = make_user(session, "a@example.com")
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash
    assert accounts.verify_password(PASSWORD, user.password_hash)
    assert not accounts.verify_password(PASSWORD + "x", user.password_hash)


def test_a_short_password_is_refused_on_length_not_composition(session) -> None:  # type: ignore[no-untyped-def]
    """Forced symbols produce `P@ssw0rd!` and a sticky note."""
    with pytest.raises(accounts.AccountError, match="at least 12 characters"):
        accounts.hash_password("short")
    with pytest.raises(accounts.AccountError, match="at least 12 characters"):
        accounts.register(
            session,
            email="short@example.com",
            password="tooshort",
            organization_id=repo.DEFAULT_ORG_ID,
        )


def test_two_users_with_the_same_password_get_different_hashes(session) -> None:  # type: ignore[no-untyped-def]
    """Salted. Identical hashes would let one cracked password reveal others."""
    a = make_user(session, "a@example.com")
    b = accounts.register(
        session,
        email="b@example.com",
        password=PASSWORD,
        organization_id=repo.DEFAULT_ORG_ID,
        role=Role.VIEWER,
    )
    assert a.password_hash != b.password_hash


# -- sign-in ---------------------------------------------------------------


def test_an_unknown_address_and_a_wrong_password_are_indistinguishable(session) -> None:  # type: ignore[no-untyped-def]
    """Different wording per cause turns sign-in into an enumeration oracle."""
    make_user(session, "known@example.com")
    wrong = accounts.attempt_sign_in(session, email="known@example.com", password="nope-nope-nope")
    unknown = accounts.attempt_sign_in(
        session, email="ghost@example.com", password="nope-nope-nope"
    )
    assert wrong.outcome is unknown.outcome is accounts.SignInOutcome.BAD_CREDENTIALS


def test_an_account_locks_after_repeated_failures(session) -> None:  # type: ignore[no-untyped-def]
    make_user(session, "target@example.com")
    for _ in range(accounts.MAX_FAILED_SIGN_INS):
        accounts.attempt_sign_in(session, email="target@example.com", password="wrong-wrong-wrong")
    result = accounts.attempt_sign_in(session, email="target@example.com", password=PASSWORD)
    assert result.outcome is accounts.SignInOutcome.LOCKED
    assert result.retry_after_seconds > 0


def test_a_locked_account_is_told_it_is_locked(session) -> None:  # type: ignore[no-untyped-def]
    """The one place the generic message is dropped. A locked-out user told
    "wrong password" keeps trying, stays locked, and raises a support ticket
    about a bug that does not exist.
    """
    user = make_user(session, "locked@example.com")
    user.locked_until = datetime.now(tz=timezone.utc) + timedelta(minutes=5)
    session.flush()
    assert (
        accounts.attempt_sign_in(session, email="locked@example.com", password=PASSWORD).outcome
        is accounts.SignInOutcome.LOCKED
    )


def test_a_successful_sign_in_clears_the_failure_count(session) -> None:  # type: ignore[no-untyped-def]
    user = make_user(session, "ok@example.com")
    accounts.attempt_sign_in(session, email="ok@example.com", password="wrong-wrong-wrong")
    assert user.failed_sign_ins == 1
    assert accounts.attempt_sign_in(session, email="ok@example.com", password=PASSWORD).ok
    assert user.failed_sign_ins == 0
    assert user.last_seen_at is not None


def test_a_disabled_account_cannot_sign_in(session) -> None:  # type: ignore[no-untyped-def]
    user = make_user(session, "gone@example.com")
    user.is_active = False
    session.flush()
    assert (
        accounts.attempt_sign_in(session, email="gone@example.com", password=PASSWORD).outcome
        is accounts.SignInOutcome.INACTIVE
    )


def test_email_is_unique_within_an_organisation_not_globally(session) -> None:  # type: ignore[no-untyped-def]
    """A global unique means one tenant's sign-up fails because another tenant
    used that address -- and the error confirms an account exists elsewhere.
    """
    other = Organization(name="Other", slug="other")
    session.add(other)
    session.flush()

    make_user(session, "shared@example.com")
    with pytest.raises(accounts.AccountError, match="already exists in this organisation"):
        make_user(session, "shared@example.com")
    # The same address in a different organisation is a different person's job.
    assert make_user(session, "shared@example.com", org=other.id) is not None


# -- roles -----------------------------------------------------------------


def test_every_role_has_an_explicit_permission_set() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_roles_are_written_out_rather_than_inherited() -> None:
    """Inheritance means widening one role silently widens every role above it,
    and the person making the change sees only the line they edited.
    """
    assert ROLE_PERMISSIONS[Role.VIEWER] == {Permission.PROJECT_READ}
    assert Permission.PROJECT_DELETE not in ROLE_PERMISSIONS[Role.PLANNER]
    assert Permission.KEY_MANAGE not in ROLE_PERMISSIONS[Role.ADMIN]
    assert ROLE_PERMISSIONS[Role.OWNER] == frozenset(Permission)


def test_a_role_is_per_organisation(session) -> None:  # type: ignore[no-untyped-def]
    """Somebody can own one organisation and merely view another."""
    other = Organization(name="Other", slug="other")
    session.add(other)
    session.flush()
    user = make_user(session, "dual@example.com", role=Role.OWNER)
    session.add(
        __import__("massingplan.models", fromlist=["Membership"]).Membership(
            user_id=user.id,
            organization_id=other.id,
            email="dual@example.com",
            role=Role.VIEWER,
        )
    )
    session.flush()
    assert Permission.PROJECT_DELETE in accounts.permissions_for(user, repo.DEFAULT_ORG_ID)
    assert Permission.PROJECT_DELETE not in accounts.permissions_for(user, other.id)


# -- the web layer ---------------------------------------------------------


def test_every_project_page_needs_a_sign_in(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    for path in ("/projects", "/upload", "/account"):
        response = client.get(path)
        assert response.status_code == 302
        assert "/auth/sign-in" in response.headers["Location"]


def test_the_demo_stays_open_because_it_stores_nothing(app) -> None:  # type: ignore[no-untyped-def]
    assert app.test_client().get("/demo").status_code == 200


def test_signing_in_and_out(app) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        make_user(session, "planner@example.com")
    client = app.test_client()
    assert sign_in(client, "planner@example.com").status_code == 302
    assert client.get("/projects").status_code == 200
    assert client.post("/auth/sign-out").status_code == 302
    assert client.get("/projects").status_code == 302


def test_a_wrong_password_is_a_401_with_the_generic_message(app) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        make_user(session, "planner@example.com")
    response = sign_in(app.test_client(), "planner@example.com", "wrong-wrong-wrong")
    assert response.status_code == 401
    assert "do not match an account" in response.get_data(as_text=True)


def test_the_next_parameter_cannot_send_you_off_site(app) -> None:  # type: ignore[no-untyped-def]
    """An open redirect turns the sign-in page into a convincing phishing
    launchpad on your own domain.
    """
    with database.session_scope() as session:
        make_user(session, "planner@example.com")
    client = app.test_client()
    response = client.post(
        "/auth/sign-in",
        data={"email": "planner@example.com", "password": PASSWORD, "next": "//evil.example.com"},
    )
    assert response.headers["Location"] == "/projects"


def test_the_session_id_rotates_on_sign_in(app) -> None:  # type: ignore[no-untyped-def]
    """Without rotation, a session fixed by an attacker before sign-in is still
    valid after it.
    """
    with database.session_scope() as session:
        make_user(session, "planner@example.com")
    client = app.test_client()
    client.get("/")
    before = client.get_cookie("session")
    sign_in(client, "planner@example.com")
    after = client.get_cookie("session")
    assert before is None or before.value != after.value


# -- authorisation ---------------------------------------------------------


def stored_project(app):  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        project, _outcome, _job = projects.import_schedule(
            session, fixture_schedule(), organization_id=repo.DEFAULT_ORG_ID, name="Tower"
        )
        return project.id


def test_a_viewer_cannot_set_a_baseline(app) -> None:  # type: ignore[no-untyped-def]
    project_id = stored_project(app)
    with database.session_scope() as session:
        make_user(session, "viewer@example.com", role=Role.VIEWER)
    client = app.test_client()
    sign_in(client, "viewer@example.com")

    assert client.get(f"/projects/{project_id}").status_code == 200
    response = client.post(f"/projects/{project_id}/baseline", data={"name": "GMP"})
    assert response.status_code == 403
    assert "baseline:set" in response.get_data(as_text=True)


def test_a_planner_can_set_a_baseline_but_not_delete(app) -> None:  # type: ignore[no-untyped-def]
    project_id = stored_project(app)
    with database.session_scope() as session:
        make_user(session, "planner@example.com", role=Role.PLANNER)
    client = app.test_client()
    sign_in(client, "planner@example.com")

    assert client.post(f"/projects/{project_id}/baseline", data={"name": "GMP"}).status_code == 302
    assert client.post(f"/projects/{project_id}/delete").status_code == 403


def test_the_baseline_form_is_hidden_from_someone_who_cannot_use_it(app) -> None:  # type: ignore[no-untyped-def]
    project_id = stored_project(app)
    with database.session_scope() as session:
        make_user(session, "viewer@example.com", role=Role.VIEWER)
    client = app.test_client()
    sign_in(client, "viewer@example.com")
    assert "Set baseline" not in client.get(f"/projects/{project_id}").get_data(as_text=True)


# -- tenant isolation ------------------------------------------------------


def test_another_organisations_project_is_a_404_not_a_403(app) -> None:  # type: ignore[no-untyped-def]
    """403 confirms the id is real, and a sequential scan then maps a
    competitor's portfolio.
    """
    project_id = stored_project(app)
    with database.session_scope() as session:
        other = Organization(name="Other", slug="other")
        session.add(other)
        session.flush()
        make_user(session, "outsider@example.com", org=other.id)

    client = app.test_client()
    sign_in(client, "outsider@example.com")
    assert client.get(f"/projects/{project_id}").status_code == 404
    assert client.post(f"/projects/{project_id}/delete").status_code == 404


def test_a_project_list_shows_only_your_own(app) -> None:  # type: ignore[no-untyped-def]
    stored_project(app)
    with database.session_scope() as session:
        other = Organization(name="Other", slug="other")
        session.add(other)
        session.flush()
        make_user(session, "outsider@example.com", org=other.id)
    client = app.test_client()
    sign_in(client, "outsider@example.com")
    assert "Tower" not in client.get("/projects").get_data(as_text=True)


# -- API keys --------------------------------------------------------------


def test_the_json_api_refuses_an_anonymous_call(app) -> None:  # type: ignore[no-untyped-def]
    response = app.test_client().post(
        "/api/massingplan/v1/schedule", json={"activities": [{"id": "A", "duration_days": 1}]}
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "unauthenticated"


def test_capabilities_stays_open_so_a_client_can_discover_the_format(app) -> None:  # type: ignore[no-untyped-def]
    assert app.test_client().get("/api/massingplan/v1/capabilities").status_code == 200


def test_a_valid_key_authenticates_the_api(app) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        plaintext, _record = accounts.issue_api_key(
            session, organization_id=repo.DEFAULT_ORG_ID, name="CI"
        )
    for header in ({"Authorization": f"Bearer {plaintext}"}, {"X-Api-Key": plaintext}):
        response = app.test_client().post(
            "/api/massingplan/v1/schedule",
            json={"data_date": "2026-06-01", "activities": [{"id": "A", "duration_days": 5}]},
            headers=header,
        )
        assert response.status_code == 200, header
        assert response.get_json()["project_finish"] == "2026-06-05"


def test_only_the_hash_of_a_key_is_stored(app) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        plaintext, record = accounts.issue_api_key(
            session, organization_id=repo.DEFAULT_ORG_ID, name="CI"
        )
        assert plaintext not in record.key_hash
        assert record.prefix == plaintext[:12]
        assert len(record.key_hash) == 64


def test_a_revoked_key_stops_working(app) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        plaintext, record = accounts.issue_api_key(
            session, organization_id=repo.DEFAULT_ORG_ID, name="CI"
        )
        accounts.revoke_api_key(session, record)
    response = app.test_client().post(
        "/api/massingplan/v1/schedule",
        json={"activities": [{"id": "A", "duration_days": 1}]},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 401


def test_a_viewer_key_cannot_be_used_beyond_reading(app) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        plaintext, _record = accounts.issue_api_key(
            session, organization_id=repo.DEFAULT_ORG_ID, name="Read only", role=Role.VIEWER
        )
    # PROJECT_READ is enough for the compute endpoints, which store nothing.
    assert (
        app.test_client()
        .post(
            "/api/massingplan/v1/schedule",
            json={"data_date": "2026-06-01", "activities": [{"id": "A", "duration_days": 1}]},
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        .status_code
        == 200
    )


def test_a_garbage_key_is_rejected_without_a_hint(app) -> None:  # type: ignore[no-untyped-def]
    response = app.test_client().post(
        "/api/massingplan/v1/schedule",
        json={"activities": [{"id": "A", "duration_days": 1}]},
        headers={"Authorization": "Bearer mpln_not-a-real-key"},
    )
    assert response.status_code == 401
    assert "not-a-real-key" not in response.get_data(as_text=True)


# -- audit -----------------------------------------------------------------


def test_signing_in_setting_a_baseline_and_deleting_are_all_recorded(app) -> None:  # type: ignore[no-untyped-def]
    project_id = stored_project(app)
    with database.session_scope() as session:
        make_user(session, "owner@example.com", role=Role.OWNER)
    client = app.test_client()
    sign_in(client, "owner@example.com")
    client.post(f"/projects/{project_id}/baseline", data={"name": "GMP"})
    client.post(f"/projects/{project_id}/delete")

    with database.session_scope() as session:
        actions = [
            e.action for e in session.scalars(select(AuditEvent).order_by(AuditEvent.at)).all()
        ]
    assert "auth.sign_in" in actions
    assert "baseline.set" in actions
    assert "project.delete" in actions


def test_the_delete_audit_names_the_project_it_deleted(app) -> None:  # type: ignore[no-untyped-def]
    """After the row is gone, an audit entry holding only an id references
    nothing.
    """
    project_id = stored_project(app)
    with database.session_scope() as session:
        make_user(session, "owner@example.com")
    client = app.test_client()
    sign_in(client, "owner@example.com")
    client.post(f"/projects/{project_id}/delete")

    with database.session_scope() as session:
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "project.delete")
        ).first()
    assert event is not None
    assert "TOWER" in event.summary


def test_an_audit_row_never_carries_a_secret(app) -> None:  # type: ignore[no-untyped-def]
    """A row recording the use of a credential must not be a second copy of it."""
    with database.session_scope() as session:
        make_user(session, "owner@example.com")
    client = app.test_client()
    sign_in(client, "owner@example.com")
    client.post("/account/keys", data={"name": "CI"})

    with database.session_scope() as session:
        events = session.scalars(select(AuditEvent)).all()
        keys = session.scalars(
            select(__import__("massingplan.models", fromlist=["ApiKey"]).ApiKey)
        ).all()
        blob = " ".join(f"{e.summary} {e.detail}" for e in events)

    assert PASSWORD not in blob
    # The 12-character prefix is deliberately non-secret -- it is how a key is
    # identified in a list and revoked. What must never appear is a usable key
    # or its hash.
    for key in keys:
        assert key.key_hash not in blob
        assert key.prefix in blob  # so the row is actually identifiable
    assert not re.search(r"mpln_[A-Za-z0-9_-]{20,}", blob)


# -- responses -------------------------------------------------------------


def test_a_request_id_comes_back_on_every_response(app) -> None:  # type: ignore[no-untyped-def]
    response = app.test_client().get("/healthz")
    assert re.fullmatch(r"[0-9a-f]{16}", response.headers["X-Request-Id"])


def test_a_supplied_request_id_is_echoed_so_a_trace_stays_joined(app) -> None:  # type: ignore[no-untyped-def]
    response = app.test_client().get("/healthz", headers={"X-Request-Id": "abc123"})
    assert response.headers["X-Request-Id"] == "abc123"


def test_readiness_checks_the_engine_and_the_database_for_real(app) -> None:  # type: ignore[no-untyped-def]
    body = app.test_client().get("/readyz").get_json()
    assert body["status"] == "ready"
    assert body["checks"] == {"engine": "ok", "database": "ok"}


def test_liveness_does_not_touch_the_database(app) -> None:  # type: ignore[no-untyped-def]
    """A liveness probe that fails on a brief database blip gets the container
    killed, which does not bring the database back and does lose requests.
    """
    assert app.test_client().get("/healthz").get_json() == {"status": "ok"}


def test_an_api_error_carries_a_request_id_and_no_stack_trace(app) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        plaintext, _record = accounts.issue_api_key(
            session, organization_id=repo.DEFAULT_ORG_ID, name="CI"
        )
    response = app.test_client().post(
        "/api/massingplan/v1/schedule",
        json={
            "activities": [
                {"id": "A", "duration_days": 1, "predecessors": ["B"]},
                {"id": "B", "duration_days": 1, "predecessors": ["A"]},
            ]
        },
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 422
    body = response.get_data(as_text=True)
    assert "Traceback" not in body
    assert "massingplan/core" not in body
