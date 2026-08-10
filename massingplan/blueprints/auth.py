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
from ..services import accounts
from ..services import repository as repo
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
    # Only relative paths. An open redirect turns the sign-in page into a
    # convincing launchpad for a phishing link on your own domain.
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect(url_for("main.projects_list"))


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

    try:
        with deps.committing() as session:
            if organization_name:
                # A new organisation, and the registrant owns it.
                slug = organization_name.lower().replace(" ", "-")[:80]
                existing = session.scalars(
                    __import__("sqlalchemy").select(Organization).where(Organization.slug == slug)
                ).first()
                if existing is not None:
                    raise AccountError("an organisation with that name already exists")
                organization = Organization(name=organization_name, slug=slug)
                session.add(organization)
                session.flush()
            else:
                organization = repo.ensure_default_organization(session)

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
