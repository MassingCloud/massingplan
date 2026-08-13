"""The SSO flow across two requests, where the session carries the secrets.

`authenticate()` and every token check are unit-tested in `test_oidc.py`. What
this covers is the wiring: that the three secrets are parked, checked and
cleared, that a first sign-in provisions into a tenant of its own, and that a
callback nobody started is refused.

The last one is login CSRF, and it is the reason the state check exists. An
attacker who can make a victim's browser hit the callback carrying the
*attacker's* code signs the victim into the attacker's account -- at which
point everything the victim then uploads is readable by the attacker. It is a
quiet bug, because from the victim's side the application works.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from massingplan import database
from massingplan.app import create_app
from massingplan.blueprints import auth as auth_bp
from massingplan.config import Settings
from massingplan.models.identity import User
from massingplan.services import repository as repo

# Same reason as `test_oidc.py`: the `no-adapters` job deletes the adapter, and
# these tests are about the adapter.
oidc = pytest.importorskip(
    "massingplan.services.identity.oidc",
    reason="the OIDC adapter is absent (the no-adapters job deletes it)",
)

from .test_oidc import CLIENT_ID, ISSUER, REDIRECT, SECRET, FakeIdp  # noqa: E402


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def sso_app(tmp_path, idp: FakeIdp):  # type: ignore[no-untyped-def]
    application = create_app(
        Settings(
            env="testing",
            secret_key="sso-test-key",
            database_url=f"sqlite:///{tmp_path / 'sso.db'}",
            rate_limit_enabled=False,
            oidc_issuer=ISSUER,
            oidc_client_id=CLIENT_ID,
            oidc_client_secret=SECRET,
            oidc_redirect_uri=REDIRECT,
        )
    )
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    database.create_all()
    with database.session_scope() as session:
        repo.ensure_default_organization(session)

    # The provider the routes build reaches the fake issuer instead of the
    # network. Patched at the factory so both legs get one, and the offline
    # guarantee holds without standing up a server.
    original = auth_bp._sso_provider

    def fake_provider():  # type: ignore[no-untyped-def]
        return oidc.OidcProvider(
            oidc.OidcSettings(
                issuer=ISSUER,
                client_id=CLIENT_ID,
                client_secret=SECRET,
                redirect_uri=REDIRECT,
            ),
            fetcher=idp.fetch,  # type: ignore[arg-type]
        )

    auth_bp._sso_provider = fake_provider  # type: ignore[assignment]
    try:
        yield application
    finally:
        auth_bp._sso_provider = original  # type: ignore[assignment]


@pytest.fixture
def plain_app(tmp_path):  # type: ignore[no-untyped-def]
    """The same app with no OIDC settings at all."""
    application = create_app(
        Settings(
            env="testing",
            secret_key="plain-key",
            database_url=f"sqlite:///{tmp_path / 'plain.db'}",
            rate_limit_enabled=False,
        )
    )
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    database.create_all()
    with database.session_scope() as session:
        repo.ensure_default_organization(session)
    return application


def _begin(client) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    response = client.get("/auth/sso")
    assert response.status_code == 302, response.get_data(as_text=True)[:300]
    with client.session_transaction() as session:
        return session["sso_state"], session["sso_nonce"]


# -- when it is off --------------------------------------------------------


def test_the_sign_in_page_offers_sso_only_when_it_is_configured(sso_app, plain_app) -> None:  # type: ignore[no-untyped-def]
    """A button that leads somewhere half-configured fails at the issuer, where
    the operator cannot see why.
    """
    assert "/auth/sso" in sso_app.test_client().get("/auth/sign-in").get_data(as_text=True)
    assert "/auth/sso" not in plain_app.test_client().get("/auth/sign-in").get_data(as_text=True)


def test_the_routes_refuse_rather_than_half_work_when_unconfigured(plain_app) -> None:  # type: ignore[no-untyped-def]
    client = plain_app.test_client()
    assert client.get("/auth/sso").status_code == 404
    assert client.get("/auth/sso/callback?code=x&state=y").status_code == 404


# -- leg one ---------------------------------------------------------------


def test_starting_a_sign_in_parks_three_secrets_and_redirects(sso_app) -> None:  # type: ignore[no-untyped-def]
    client = sso_app.test_client()
    response = client.get("/auth/sso")
    assert response.status_code == 302
    assert response.headers["Location"].startswith(f"{ISSUER}/authorize")

    with client.session_transaction() as session:
        assert session["sso_state"]
        assert session["sso_nonce"]
        assert session["sso_verifier"]


# -- leg three -------------------------------------------------------------


def test_a_first_sign_in_provisions_into_a_tenant_of_its_own(sso_app, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    """Never the default organisation.

    A verified assertion that somebody controls an email address is not a
    statement about who they work for. The version of that mistake where
    strangers became owners of the seeded tenant has already shipped here once,
    through the registration form.
    """
    client = sso_app.test_client()
    state, nonce = _begin(client)
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(nonce=nonce)})

    response = client.get(f"/auth/sso/callback?code=the-code&state={state}")
    assert response.status_code == 302, response.get_data(as_text=True)[:500]

    with database.session_scope() as session:
        user = session.scalars(select(User).where(User.sso_subject == f"{ISSUER}#user-42")).first()
        assert user is not None
        assert user.email == "planner@example.com"
        assert user.memberships[0].organization_id != repo.DEFAULT_ORG_ID


def test_the_provisioned_account_has_no_usable_password(sso_app, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    """An SSO account must not also be reachable by guessing a password -- and
    the hash must not be a fixed sentinel shared by every such account.
    """
    client = sso_app.test_client()
    state, nonce = _begin(client)
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(nonce=nonce)})
    client.get(f"/auth/sso/callback?code=c&state={state}")

    with database.session_scope() as session:
        user = session.scalars(select(User).where(User.sso_subject.is_not(None))).first()
        assert user is not None
        assert user.password_hash
        stored = user.password_hash

    signed_in = sso_app.test_client().post(
        "/auth/sign-in", data={"email": "planner@example.com", "password": ""}
    )
    assert signed_in.status_code in (400, 401)
    assert stored.startswith("$argon2")


def test_a_second_sign_in_reuses_the_account(sso_app, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    """Matched on subject. A second account per sign-in would split somebody's
    projects across tenants they cannot see into.
    """
    for _ in range(2):
        client = sso_app.test_client()
        state, nonce = _begin(client)
        idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(nonce=nonce)})
        assert client.get(f"/auth/sso/callback?code=c&state={state}").status_code == 302

    with database.session_scope() as session:
        users = session.scalars(select(User).where(User.sso_subject.is_not(None))).all()
    assert len(users) == 1


def test_a_changed_email_does_not_create_a_second_account(sso_app, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    """The reason matching is on `sub` and not on the address: people change
    their email, and a reallocated mailbox must not become a new identity.
    """
    client = sso_app.test_client()
    state, nonce = _begin(client)
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(nonce=nonce)})
    client.get(f"/auth/sso/callback?code=c&state={state}")

    second = sso_app.test_client()
    state, nonce = _begin(second)
    idp.serve(
        f"{ISSUER}/token",
        200,
        {"id_token": idp.id_token(nonce=nonce, email="new.address@example.com")},
    )
    assert second.get(f"/auth/sso/callback?code=c&state={state}").status_code == 302

    with database.session_scope() as session:
        users = session.scalars(select(User).where(User.sso_subject.is_not(None))).all()
    assert len(users) == 1


# -- the attacks on the callback -------------------------------------------


def test_a_callback_nobody_started_is_refused(sso_app, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    """Login CSRF. No session state means nothing to match, and it must fail
    closed rather than treating "" == "" as a match.
    """
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token()})
    response = sso_app.test_client().get("/auth/sso/callback?code=c&state=anything")
    assert response.status_code == 400
    assert "state" in response.get_data(as_text=True)

    with database.session_scope() as session:
        assert session.scalars(select(User).where(User.sso_subject.is_not(None))).all() == []


def test_a_forged_state_is_refused(sso_app, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    client = sso_app.test_client()
    _begin(client)
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token()})
    assert client.get("/auth/sso/callback?code=c&state=forged").status_code == 400


def test_the_secrets_are_cleared_even_when_the_callback_fails(sso_app) -> None:  # type: ignore[no-untyped-def]
    """A leftover state is a second chance at a replay."""
    client = sso_app.test_client()
    _begin(client)
    client.get("/auth/sso/callback?code=c&state=wrong")
    with client.session_transaction() as session:
        assert "sso_state" not in session
        assert "sso_nonce" not in session
        assert "sso_verifier" not in session


def test_a_replayed_callback_cannot_be_used_twice(sso_app, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    client = sso_app.test_client()
    state, nonce = _begin(client)
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(nonce=nonce)})
    assert client.get(f"/auth/sso/callback?code=c&state={state}").status_code == 302
    assert client.get(f"/auth/sso/callback?code=c&state={state}").status_code == 400


def test_a_token_for_another_nonce_is_refused_at_the_route(sso_app, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    """The unit test covers the check; this covers that the route actually
    passes the session's nonce into it rather than the token's own.
    """
    client = sso_app.test_client()
    state, _nonce = _begin(client)
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(nonce="somebody-elses")})
    response = client.get(f"/auth/sso/callback?code=c&state={state}")
    assert response.status_code == 400
    assert "does not answer this sign-in" in response.get_data(as_text=True)


def test_an_sso_user_with_no_organisation_gets_the_same_answer_as_a_password_user(
    sso_app, idp: FakeIdp
) -> None:  # type: ignore[no-untyped-def]
    """One state, one answer.

    An owner removing somebody's membership leaves an account that can still
    authenticate and belongs nowhere. The password path has always answered
    that with a 403 and a sentence telling them what to do. The SSO path raised
    `AccountError` out of the route, so the same person got a 500 and a request
    id -- and no idea that a membership was the problem.
    """
    from massingplan.models.identity import Membership

    client = sso_app.test_client()
    state, nonce = _begin(client)
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(nonce=nonce)})
    assert client.get(f"/auth/sso/callback?code=c&state={state}").status_code == 302

    with database.session_scope() as session:
        user = session.scalars(select(User).where(User.sso_subject.is_not(None))).one()
        for membership in list(user.memberships):
            session.delete(session.get(Membership, membership.id))

    second = sso_app.test_client()
    state, nonce = _begin(second)
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(nonce=nonce)})
    response = second.get(f"/auth/sso/callback?code=c&state={state}")

    assert response.status_code == 403, "a 500 here tells the user nothing"
    assert "not a member of any organisation" in response.get_data(as_text=True)


def test_a_disabled_sso_account_is_refused(sso_app, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    """Deactivating an account has to stop the SSO door too, or it only ever
    closed the one with a password on it.
    """
    client = sso_app.test_client()
    state, nonce = _begin(client)
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(nonce=nonce)})
    assert client.get(f"/auth/sso/callback?code=c&state={state}").status_code == 302

    with database.session_scope() as session:
        user = session.scalars(select(User).where(User.sso_subject.is_not(None))).one()
        user.is_active = False

    second = sso_app.test_client()
    state, nonce = _begin(second)
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(nonce=nonce)})
    response = second.get(f"/auth/sso/callback?code=c&state={state}")

    assert response.status_code == 403
    assert "disabled" in response.get_data(as_text=True)


def test_an_error_from_the_issuer_is_not_echoed(sso_app) -> None:  # type: ignore[no-untyped-def]
    """`error_description` is attacker-influenced text on a page we render."""
    client = sso_app.test_client()
    _begin(client)
    response = client.get(
        "/auth/sso/callback?error=access_denied&error_description=%3Cscript%3Ealert(1)%3C/script%3E"
    )
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "alert(1)" not in body
    assert "refused the sign-in" in body
