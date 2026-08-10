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
from ..services import accounts, mfa
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
