"""Sign in, sign out, register, switch organisation.

Every failure returns the same message and the same status, with one deliberate
exception: a locked account is told it is locked. A locked-out user told "wrong
password" keeps trying, stays locked, and raises a support ticket about a bug
that does not exist.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, redirect, render_template, request, url_for

from ..models import Organization
from ..models.identity import Role
from ..services import accounts, identity, mfa
from ..services.accounts import AccountError, SignInOutcome
from . import deps

bp = Blueprint("auth", __name__, url_prefix="/auth")

#: One message for every credential failure. Distinct wording per cause turns
#: the sign-in form into an account-enumeration oracle.
GENERIC_FAILURE = "That email and password do not match an account."


@bp.get("/sign-in")
def sign_in_page() -> Any:
    if deps.current_principal().is_authenticated:
        return redirect(url_for("main.projects_list"))
    return render_template("auth/sign_in.html", error=None, next=request.args.get("next", ""))


@bp.post("/sign-in")
def sign_in() -> Any:
    email = request.form.get("email", "")
    password = request.form.get("password", "")
    target = request.form.get("next", "")

    with deps.committing() as session:
        result = accounts.attempt_sign_in(session, email=email, password=password)

    if result.outcome is SignInOutcome.LOCKED:
        minutes = max(1, result.retry_after_seconds // 60)
        return render_template(
            "auth/sign_in.html",
            error=(
                f"This account is locked after too many attempts. Try again in {minutes} minutes."
            ),
            next=target,
        ), 429
    if not result.ok or result.user is None:
        return render_template("auth/sign_in.html", error=GENERIC_FAILURE, next=target), 401

    membership = result.user.memberships[0] if result.user.memberships else None
    if membership is None:
        return render_template(
            "auth/sign_in.html",
            error="This account is not a member of any organisation. Ask an owner to invite you.",
            next=target,
        ), 403

    if mfa.is_enabled(result.user):
        # Password accepted, and that is all it is. The session holds a user id
        # and no authenticated principal, so nothing behind `login_required` is
        # reachable until the second factor verifies.
        deps.begin_mfa(result.user)
        return redirect(url_for("auth.mfa_challenge_page", next=target))

    deps.sign_in(result.user, membership.organization_id)
    with deps.committing() as session:
        accounts.audit(
            session,
            organization_id=membership.organization_id,
            action="auth.sign_in",
            actor_id=result.user.id,
            actor_label=result.user.email,
            summary="signed in",
        )
    return redirect(deps.safe_next(target))


@bp.post("/sign-out")
def sign_out() -> Any:
    deps.sign_out()
    return redirect(url_for("main.index"))


# -- single sign-on --------------------------------------------------------
#
# Three server-side secrets are parked in the session between the two legs, and
# every one of them is checked on the way back: `state` against a forged
# callback, `nonce` against a replayed id_token, `code_verifier` against an
# intercepted code. They are removed whether the callback succeeds or fails --
# a leftover state is a second chance for a replay.

SSO_STATE = "sso_state"
SSO_NONCE = "sso_nonce"
SSO_VERIFIER = "sso_verifier"


def _sso_settings() -> Any:
    from flask import current_app

    return current_app.extensions["massingplan_settings"]


def _sso_provider() -> Any:
    """The configured provider, or `None` when SSO is off.

    Through `identity.resolve` rather than importing the adapter, which is what
    import-linter enforces and what makes a deployment missing that adapter
    fail with an install hint instead of an `ImportError` at request time.

    Built per request rather than held on the app: the discovery cache lives on
    the instance, and a process-wide one would be a cache nothing invalidates
    when the operator changes the issuer.
    """
    settings = _sso_settings()
    if not settings.sso_enabled:
        return None
    return identity.resolve(
        "oidc",
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        redirect_uri=settings.oidc_redirect_uri,
        require_tls=settings.oidc_require_tls,
    )


@bp.get("/sso")
def sso_start() -> Any:
    """Leg one: park the three secrets and send them to the issuer."""
    from flask import session as flask_session

    provider = _sso_provider()
    if provider is None:
        return render_template("auth/sign_in.html", error="SSO is not configured.", next=""), 404

    try:
        request_ = provider.begin()
    except identity.IdentityError as exc:
        return render_template("auth/sign_in.html", error=str(exc), next=""), 502

    flask_session[SSO_STATE] = request_.state
    flask_session[SSO_NONCE] = request_.nonce
    flask_session[SSO_VERIFIER] = request_.code_verifier
    return redirect(request_.url)


@bp.get("/sso/callback")
def sso_callback() -> Any:
    """Leg three: check everything, then provision or sign in.

    The session values are popped *first*, so every path below -- success,
    refusal, exception -- leaves nothing behind for a second attempt to reuse.
    """
    from flask import session as flask_session

    provider = _sso_provider()
    if provider is None:
        return render_template("auth/sign_in.html", error="SSO is not configured.", next=""), 404

    expected_state = flask_session.pop(SSO_STATE, "")
    nonce = flask_session.pop(SSO_NONCE, "")
    verifier = flask_session.pop(SSO_VERIFIER, "")

    if request.args.get("error"):
        # The issuer refused. Its own description is not echoed: it is
        # attacker-influenced text on a page we render.
        return render_template(
            "auth/sign_in.html", error="The identity provider refused the sign-in.", next=""
        ), 400

    try:
        principal = provider.authenticate(
            {
                "code": request.args.get("code", ""),
                "state": request.args.get("state", ""),
                "expected_state": expected_state,
                "nonce": nonce,
                "code_verifier": verifier,
            }
        )
    except identity.IdentityError as exc:
        # The reason is shown: unlike a password failure, there is no account
        # to enumerate here, and "the token had the wrong audience" is what an
        # operator needs to fix their client configuration.
        return render_template("auth/sign_in.html", error=str(exc), next=""), 400

    if principal is None:
        return render_template(
            "auth/sign_in.html", error="That sign-in did not complete.", next=""
        ), 400

    with deps.committing() as session:
        user = accounts.find_by_sso_subject(session, principal.subject)
        if user is None:
            user, organization_id = accounts.provision_sso_user(
                session,
                subject=principal.subject,
                email=principal.email,
                display_name=principal.display_name,
                organization_name="",
            )
            accounts.audit(
                session,
                organization_id=organization_id,
                action="auth.sso_provision",
                actor_id=user.id,
                actor_label=user.email,
                summary="created an account from a verified SSO identity",
            )
        else:
            membership = next(iter(user.memberships), None)
            if membership is None:
                raise AccountError("that account has no organisation")
            organization_id = membership.organization_id
        if not user.is_active:
            return render_template(
                "auth/sign_in.html", error="That account is disabled.", next=""
            ), 403
        signed_in_user, signed_in_org = user, organization_id

    deps.sign_in(signed_in_user, signed_in_org)
    return redirect(url_for("main.projects_list"))


@bp.get("/register")
def register_page() -> Any:
    return render_template("auth/register.html", error=None)


@bp.post("/register")
def register() -> Any:
    email = request.form.get("email", "")
    password = request.form.get("password", "")
    display_name = request.form.get("display_name", "")
    organization_name = request.form.get("organization", "").strip()

    # Registration always creates a *new* organisation. It used to fall back to
    # the default one when no name was given, and give the registrant OWNER of
    # it -- so anybody who could reach this form became an owner of the tenant
    # holding every seeded and imported project, and could read all of it. Found
    # by tests/test_adversarial.py. Joining an existing organisation is by
    # invitation, which is a different route with a different check.
    if not organization_name:
        organization_name = (email.split("@")[0] or "New organisation").strip()[:120]

    try:
        with deps.committing() as session:
            slug = "".join(
                ch if ch.isalnum() or ch == "-" else "-"
                for ch in organization_name.lower().replace(" ", "-")
            )[:80].strip("-")
            existing = session.scalars(
                __import__("sqlalchemy").select(Organization).where(Organization.slug == slug)
            ).first()
            if existing is not None:
                raise AccountError(
                    "an organisation with that name already exists. Choose another, "
                    "or ask an owner there to invite you."
                )
            organization = Organization(name=organization_name, slug=slug)
            session.add(organization)
            session.flush()

            user = accounts.register(
                session,
                email=email,
                password=password,
                display_name=display_name,
                organization_id=organization.id,
                role=Role.OWNER,
            )
            accounts.audit(
                session,
                organization_id=organization.id,
                action="auth.register",
                actor_id=user.id,
                actor_label=user.email,
                summary=f"created an account in {organization.name}",
            )
            organization_id = organization.id
    except AccountError as exc:
        return render_template("auth/register.html", error=str(exc)), 400

    deps.sign_in(user, organization_id)
    return redirect(url_for("main.projects_list"))


@bp.post("/switch/<organization_id>")
@deps.login_required
def switch(organization_id: str) -> Any:
    """Change the active organisation.

    A non-member gets "no such organisation" rather than "you are not a member":
    the second confirms the organisation exists.
    """
    from flask import session as flask_session

    principal = deps.current_principal()
    user = deps.db().get(
        __import__("massingplan.models", fromlist=["User"]).User, principal.subject_id
    )
    if user is None or user.membership_in(organization_id) is None:
        return render_template(
            "error.html",
            error=type("E", (), {"message": "No such organisation.", "detail": None})(),
        ), 404
    flask_session[deps.SESSION_ORG_KEY] = organization_id
    return redirect(url_for("main.projects_list"))


# -- the second factor -----------------------------------------------------


@bp.get("/mfa")
def mfa_challenge_page() -> Any:
    if deps.pending_mfa_user_id() is None:
        return redirect(url_for("auth.sign_in_page"))
    return render_template("auth/mfa.html", error=None, next=request.args.get("next", ""))


@bp.post("/mfa")
def mfa_challenge() -> Any:
    from ..models import User

    pending = deps.pending_mfa_user_id()
    if pending is None:
        return redirect(url_for("auth.sign_in_page"))

    target = request.form.get("next", "")
    code = request.form.get("code", "")
    with deps.committing() as session:
        user = session.get(User, pending)
        if user is None or not mfa.verify_for_user(session, user, code):
            return render_template(
                "auth/mfa.html",
                error="That code did not match. Codes expire after 30 seconds.",
                next=target,
            ), 401
        membership = user.memberships[0] if user.memberships else None
        if membership is None:
            return render_template(
                "auth/mfa.html",
                error="This account is not a member of any organisation.",
                next=target,
            ), 403
        organization_id = membership.organization_id
        remaining = mfa.remaining_recovery_codes(user)
        accounts.audit(
            session,
            organization_id=organization_id,
            action="auth.mfa_verified",
            actor_id=user.id,
            actor_label=user.email,
            summary="completed the second factor",
            detail={"recovery_codes_left": remaining},
        )

    deps.sign_in(user, organization_id)
    return redirect(deps.safe_next(target))
