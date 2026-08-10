"""Per-request plumbing: the session, the principal, and the guards.

Everything Flask-specific about authentication lives here, so `services/rbac.py`
stays framework-free and massing can build a `Principal` its own way.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any, TypeVar

from flask import g, jsonify, redirect, request, session, url_for
from sqlalchemy.orm import Session

from ..api.errors import NotFound
from ..database import new_session
from ..models.identity import Permission, User
from ..services import accounts, rbac
from ..services.rbac import ANONYMOUS, Principal

F = TypeVar("F", bound=Callable[..., Any])

SESSION_USER_KEY = "user_id"
SESSION_ORG_KEY = "organization_id"
#: Set between a correct password and a correct second factor. It holds a user
#: id and nothing else -- the user is *not* signed in while it is set, so a
#: half-authenticated session cannot reach a project.
SESSION_PENDING_MFA_KEY = "pending_mfa_user_id"


def db() -> Session:
    """The request's session, opened once and closed by the teardown hook."""
    if "db_session" not in g:
        g.db_session = new_session()
    return g.db_session


def close_db(_exc: BaseException | None = None) -> None:
    handle: Session | None = g.pop("db_session", None)
    if handle is None:
        return
    # Rollback before close, always. A request that raised leaves an open
    # transaction otherwise, and the connection goes back to the pool carrying
    # it -- so the *next* request inherits a failed transaction it never made.
    handle.rollback()
    handle.close()


@contextmanager
def committing() -> Iterator[Session]:
    """A write, committed on success and rolled back on anything else."""
    handle = db()
    try:
        yield handle
        handle.commit()
    except Exception:
        handle.rollback()
        raise


def _principal_from_api_key() -> Principal | None:
    header = request.headers.get("Authorization", "")
    presented = ""
    if header.lower().startswith("bearer "):
        presented = header[7:].strip()
    presented = presented or request.headers.get("X-Api-Key", "").strip()
    if not presented:
        return None
    record = accounts.authenticate_api_key(db(), presented)
    if record is None:
        return None
    return rbac.principal_for_api_key(record)


def _principal_from_session() -> Principal | None:
    user_id = session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    user = db().get(User, user_id)
    if user is None or not user.is_active:
        # The account went away or was disabled while the cookie lived on.
        session.clear()
        return None
    organization_id = session.get(SESSION_ORG_KEY)
    membership = user.membership_in(organization_id) if organization_id else None
    if membership is None:
        # Membership revoked, or the cookie names an organisation they left.
        # Fall back to any organisation they *are* in rather than signing them
        # out -- being removed from one project team is not a sign-out event.
        membership = user.memberships[0] if user.memberships else None
        if membership is None:
            return None
        session[SESSION_ORG_KEY] = membership.organization_id
    return rbac.principal_for_user(user, membership.organization_id)


def load_principal() -> None:
    """Resolve the caller once per request.

    Roles are read fresh every time rather than cached in the cookie: a role
    change has to take effect on the next request, not at the next sign-in.
    """
    g.principal = _principal_from_api_key() or _principal_from_session() or ANONYMOUS


def current_principal() -> Principal:
    return g.get("principal", ANONYMOUS)


def current_org() -> str | None:
    return current_principal().organization_id


def sign_in(user: User, organization_id: str) -> None:
    # Rotate the session id on privilege change. Without it, a session fixed by
    # an attacker before sign-in is still valid after it.
    session.clear()
    session[SESSION_USER_KEY] = user.id
    session[SESSION_ORG_KEY] = organization_id
    session.permanent = True


def sign_out() -> None:
    session.clear()


def begin_mfa(user: User) -> None:
    """Park the user between factors. Not signed in yet."""
    session.clear()
    session[SESSION_PENDING_MFA_KEY] = user.id


def pending_mfa_user_id() -> str | None:
    return session.get(SESSION_PENDING_MFA_KEY)


def web_session() -> Any:
    """The Flask session, behind a name.

    So that enrolment can park a half-finished secret without every blueprint
    importing `flask.session` directly and reaching for keys nobody has named.
    """
    return session


def wants_json() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def login_required(view: F) -> F:
    @wraps(view)
    def wrapper(*args: object, **kwargs: object) -> Any:
        if not current_principal().is_authenticated:
            if wants_json():
                return jsonify(
                    {
                        "error": {
                            "code": "unauthenticated",
                            "message": "sign in or present an API key",
                        }
                    }
                ), 401
            return redirect(url_for("auth.sign_in_page", next=request.path))
        return view(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def require_permission(permission: Permission) -> Callable[[F], F]:
    """Guard an action.

    Note what this does *not* do: hide the resource. Resource-level hiding is
    `load_project`'s job, which returns 404 for another tenant's id. This is for
    actions where the caller is already known and concealing the endpoint gains
    nothing -- there, "you cannot do that" is the useful answer.
    """

    def decorate(view: F) -> F:
        @wraps(view)
        def wrapper(*args: object, **kwargs: object) -> Any:
            principal = current_principal()
            if not principal.is_authenticated:
                return login_required(view)(*args, **kwargs)
            if not principal.can(permission):
                if wants_json():
                    return jsonify(
                        {
                            "error": {
                                "code": "forbidden",
                                "message": f"this account does not have {permission.value}",
                            }
                        }
                    ), 403
                from flask import render_template

                return render_template(
                    "error.html",
                    error=type(
                        "E",
                        (),
                        {
                            "message": "You do not have permission to do that.",
                            "detail": f"It needs {permission.value}; ask an owner or admin.",
                        },
                    )(),
                ), 403
            return view(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


def load_project(project_id: str) -> Any:
    """A project in the caller's organisation, or a 404.

    **404, never 403.** "This exists but is not yours" tells one contractor that
    another contractor's project id is real, and a sequential scan then maps
    their portfolio.
    """
    from ..services import repository as repo

    project = repo.get_project(db(), project_id, current_org())
    if project is None:
        raise NotFound("no such project")
    return project
